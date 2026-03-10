from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import json
import os
import time

app = Flask(__name__)
CORS(app) # Allow React frontend to fetch data

# ---------------------------------------------
# Jira Credentials — loaded from CTO DB or via /config
# NestJS calls POST /config after validating credentials
# ---------------------------------------------
jira_config = {
    "site_url":  os.environ.get("JIRA_SITE_URL", ""),
    "email":     os.environ.get("JIRA_EMAIL", ""),
    "api_token": os.environ.get("JIRA_API_TOKEN", ""),
}

@app.route('/config', methods=['POST'])
def set_config():
    """NestJS calls this after credential validation to push config into Flask."""
    data = request.get_json(silent=True) or {}
    if data.get("jira_site_url"):
        jira_config["site_url"]  = data["jira_site_url"]
        jira_config["email"]     = data.get("jira_email", jira_config["email"])
        jira_config["api_token"] = data.get("jira_api_token", jira_config["api_token"])
        print(f"\n[*] [CONFIG] Jira credentials updated -> {jira_config['site_url']} ({jira_config['email']})", flush=True)
        return jsonify({"status": "ok", "site_url": jira_config["site_url"]}), 200
    return jsonify({"error": "Missing jira_site_url"}), 400

@app.route('/config', methods=['GET'])
def get_config():
    """Return current config (without the API token for security)."""
    return jsonify({
        "site_url":   jira_config["site_url"],
        "email":      jira_config["email"],
        "configured": bool(jira_config["site_url"] and jira_config["api_token"]),
    }), 200

# ---------------------------------------------
# PostgreSQL connection (Jira metrics DB)
# ---------------------------------------------
CTO_DB_CONFIG = {
    "host":     os.environ.get("JIRA_DB_HOST",     "localhost"),
    "port":     os.environ.get("JIRA_DB_PORT",     "5432"),
    "database": os.environ.get("JIRA_DB_NAME",     "ctolocal"),
    "user":     os.environ.get("JIRA_DB_USER",     "postgres"),
    "password": os.environ.get("JIRA_DB_PASSWORD", "yuva@123"),
}

def get_db_connection():
    """Create and return a fresh DB connection."""
    return psycopg2.connect(**CTO_DB_CONFIG)

def load_jira_config_from_cto_db():
    """
    On startup: try to read Jira credentials from the CTO platform DB 
    (ctolocal) so Flask always has up-to-date creds even after restart.
    """
    try:
        cto_conn = psycopg2.connect(
            host=os.environ.get("CTO_DB_HOST", "localhost"),
            port=os.environ.get("CTO_DB_PORT", "5432"),
            database=os.environ.get("CTO_DB_NAME", "ctolocal"),
            user=os.environ.get("CTO_DB_USER", "postgres"),
            password=os.environ.get("CTO_DB_PASSWORD", "yuva@123"),
        )
        cur = cto_conn.cursor()
        cur.execute("""
            SELECT site_url, email, api_token FROM jira_integrations 
            WHERE is_verified = TRUE AND is_active = TRUE 
            ORDER BY updated_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        cto_conn.close()
        if row:
            jira_config["site_url"]  = row[0]
            jira_config["email"]     = row[1]
            jira_config["api_token"] = row[2]
            print(f"\n[OK] [STARTUP] Loaded Jira config from CTO DB -> {row[0]} ({row[1]})", flush=True)
        else:
            print("\n[!]  [STARTUP] No verified Jira credentials found in CTO DB. Use /integrations -> Connect Jira.", flush=True)
    except Exception as e:
        print(f"\n[!]  [STARTUP] Could not load Jira config from CTO DB: {e}", flush=True)


# ---------------------------------------------
# Helper to Upsert Users & Projects
# ---------------------------------------------
def upsert_user(cur, user_data):
    if not user_data or not isinstance(user_data, dict): return None
    account_id = user_data.get("accountId")
    if not account_id: return None
    
    cur.execute("""
        INSERT INTO users (jira_account_id, full_name, email, is_active, avatar_url, role)
        VALUES (%s, %s, %s, %s, %s, 'TEAM')
        ON CONFLICT (jira_account_id) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            email = EXCLUDED.email,
            is_active = EXCLUDED.is_active,
            avatar_url = EXCLUDED.avatar_url;
    """, (account_id, user_data.get("displayName"), user_data.get("emailAddress"), user_data.get("active", True), user_data.get("avatarUrls", {}).get("48x48")))
    return account_id

def upsert_project(cur, project_data):
    if not project_data or not isinstance(project_data, dict): return None
    proj_id = project_data.get("id")
    if not proj_id: return None
    
    cur.execute("""
        INSERT INTO projects (id, key, name, project_type)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            key = EXCLUDED.key,
            name = EXCLUDED.name,
            project_type = EXCLUDED.project_type;
    """, (proj_id, project_data.get("key"), project_data.get("name"), project_data.get("projectTypeKey")))
    return proj_id

# ---------------------------------------------
# Resolve CTO IDs from Mapping
# ---------------------------------------------
def resolve_cto_ids(cur, jira_project_key, jira_assignee_account_id):
    """Look up CTO hierarchy IDs based on Jira project key and assignee account ID."""
    if not jira_project_key:
        return {}
    
    try:
        cur.execute('''
            SELECT
                p.id         AS cto_project_id,
                u.account_id AS cto_account_id,
                u.market_id  AS cto_market_id,
                ji.org_id    AS cto_org_id,
                u.team_id    AS cto_team_id,
                u.id         AS cto_user_id
            FROM projects p
            LEFT JOIN users u ON u.jira_account_id = %s
            JOIN jira_integrations ji ON ji.is_active = TRUE
            WHERE p.key = %s
            LIMIT 1
        ''', [jira_assignee_account_id, jira_project_key])
        
        row = cur.fetchone()
        if row:
            return {
                'cto_project_id': row[0],
                'cto_account_id': row[1],
                'cto_market_id':  row[2],
                'cto_org_id':     row[3],
                'cto_team_id':    row[4],
                'cto_user_id':    row[5]
            }
    except Exception as e:
        print(f"\n[!] SQL Error mapping CTO IDs: {e}")
        # Reset transaction block incase of error
        try:
            cur.execute("ROLLBACK")
        except:
            pass
    return {}

# ---------------------------------------------
# Jira Webhook Endpoint
# ---------------------------------------------
@app.route('/jira/webhook', methods=['POST'])
def jira_webhook():
    # -- 1. Check Payload ------------------------
    data = request.get_json(silent=True, force=True)
    
    if data is None:
        raw_body = request.get_data(as_text=True)
        
        if not raw_body:
            content_len = request.headers.get('Content-Length', '0')
            print(f"\n[?] [DEBUG] Empty Body Received")
            print(f"   Method: {request.method}")
            print(f"   URL   : {request.url}")

            if request.args:
                data = request.args.to_dict()
            else:
                if content_len == '0':
                    print("   💓 [INFO] Heartbeat/Probe (No Content)")
                return jsonify({"status": "heartbeat", "message": "Listening..."}), 200
        else:
            try:
                data = json.loads(raw_body)
            except Exception as e:
                print(f"\n[!]  [WARN] Received Non-JSON Payload. Error: {e}")
                return jsonify({"message": "Expected JSON", "error": str(e)}), 400

    # -- 2. Identify Event Type ----------------
    event_type = data.get("webhookEvent") or data.get("issue_event_type_name") or data.get("event")
    triggered_by = data.get("triggeredByUser")
    
    if not event_type:
        if triggered_by:
            return jsonify({"status": "meta_received", "triggered_by": triggered_by}), 200
        if "test" in str(data).lower():
            return jsonify({"message": "Test successful"}), 200
        return jsonify({"message": "Payload received but no event type identified"}), 200

    # -- 3. Extract Details ----------------------
    issue_data   = data.get("issue", {})
    fields       = issue_data.get("fields", {})
    issue_id     = issue_data.get("id")
    issue_key    = issue_data.get("key") or data.get("issue_key")
    
    summary      = fields.get("summary", "No Summary")
    description  = fields.get("description")
    if isinstance(description, dict): 
        description = json.dumps(description)
    
    issue_type   = fields.get("issuetype", {}).get("name")
    status       = fields.get("status", {}).get("name")
    priority     = fields.get("priority", {}).get("name")
    created_ts   = fields.get("created")
    updated_ts   = fields.get("updated")
    due_date     = fields.get("duedate")
    story_points = fields.get("customfield_10016") or fields.get("customfield_10002")
    if story_points and not isinstance(story_points, (int, float)):
        story_points = None
    
    reporter_obj = fields.get("reporter")
    assignee_obj = fields.get("assignee")
    project_obj  = fields.get("project")

    # -- 4. Log to Terminal --------
    color_code = "\033[94m"
    event_str  = str(event_type).lower()
    if "created" in event_str:   color_code = "\033[92m"; action_name = "CREATED"
    elif "deleted" in event_str: color_code = "\033[91m"; action_name = "DELETED"
    elif "updated" in event_str: color_code = "\033[94m"; action_name = "UPDATED"
    else:                        action_name = event_type.upper()
    reset_code = "\033[0m"

    print(f"\n{color_code}--------------------------------------------------{reset_code}", flush=True)
    print(f"{color_code}[!] JIRA ACTIVITY: {action_name}{reset_code}", flush=True)
    if issue_key:
        print(f"   Issue    : {issue_key}", flush=True)
        print(f"   Summary  : {summary}", flush=True)
        print(f"   Status   : \033[1m{status}\033[0m", flush=True)
    print(f"{color_code}--------------------------------------------------{reset_code}", flush=True)

    # -- 5. Database Interaction --
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        
        # -- V3: Resolve CTO IDs --
        jira_project_key = project_obj.get('key') if project_obj else None
        jira_assignee_account_id = assignee_obj.get('accountId') if assignee_obj else None
        jira_reporter_account_id = reporter_obj.get('accountId') if reporter_obj else None
        
        cto_ids = resolve_cto_ids(cur, jira_project_key, jira_assignee_account_id)
        
        # Generate event_id from payload id or timestamp to avoid duplicates
        event_id = data.get("id") or f"{issue_id}_{event_type}_{int(time.time() * 1000)}_webhook"
        
        # Extract change tracking specifics
        changed_field = None
        old_value = None
        new_value = None
        changelog = data.get("changelog", {})
        if changelog:
            items = changelog.get("items", [])
            if items:
                # We prioritize status change, fallback to first item
                change_item = next((i for i in items if i.get("field") == "status"), items[0])
                changed_field = change_item.get("field")
                old_value = change_item.get("fromString")
                new_value = change_item.get("toString")
        
        # If it's a create event, newValue is typically issueStatus
        if not changed_field and "created" in str(event_type).lower():
            changed_field = "status"
            new_value = status

        issue_priority = dict(fields.get("priority", {})).get("name") if isinstance(fields.get("priority"), dict) else None
        
        # Extract sprint info
        sprint_info = fields.get("customfield_10020")
        sprint_id = None
        sprint_name = None
        sprint_state = None
        sprint_start_date = None
        sprint_end_date = None
        jira_board_id = None
        if sprint_info and isinstance(sprint_info, list) and len(sprint_info) > 0:
            sprint = sprint_info[0]
            if isinstance(sprint, dict):
                sprint_id = str(sprint.get('id', ''))
                sprint_name = sprint.get('name')
                sprint_state = sprint.get('state')
                sprint_start_date = sprint.get('startDate')
                sprint_end_date = sprint.get('endDate')
                jira_board_id = str(sprint.get('boardId', '')) if sprint.get('boardId') else None

        label_list = fields.get("labels", [])
        
        # Additional fields missing
        jira_project_id = project_obj.get('id') if project_obj else None
        creator_obj = fields.get("creator")
        jira_creator_account_id = creator_obj.get("accountId") if isinstance(creator_obj, dict) else None
        issue_resolved_at = fields.get("resolutiondate")

        
        # Prevent duplicate raw records: delete previous entries for this issue
        cur.execute("DELETE FROM jira_webhook_raw WHERE jira_issue_id = %s", (issue_id,))

        # Prepare row for insert
        insert_sql = """
            INSERT INTO jira_webhook_raw (
                event_id, event_type, 
                jira_issue_id, jira_issue_key, jira_project_id, jira_project_key, jira_board_id, jira_sprint_id, jira_sprint_name,
                cto_project_id, cto_account_id, cto_market_id, cto_org_id, cto_team_id, cto_user_id,
                jira_assignee_account_id, jira_reporter_account_id, jira_creator_account_id,
                issue_type, issue_status, issue_priority, story_points, labels,
                sprint_state, sprint_start_date, sprint_end_date, 
                issue_created_at, issue_updated_at, issue_resolved_at,
                changed_field, old_value, new_value,
                raw_payload
            ) VALUES (
                %s, %s, 
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, 
                %s, %s, %s,
                %s, %s, %s,
                %s
            )
            ON CONFLICT (event_id) DO NOTHING
        """
        insert_args = (
            event_id, event_type,
            issue_id, issue_key, jira_project_id, jira_project_key, jira_board_id, sprint_id, sprint_name,
            cto_ids.get('cto_project_id'), cto_ids.get('cto_account_id'), cto_ids.get('cto_market_id'), cto_ids.get('cto_org_id'), cto_ids.get('cto_team_id'), cto_ids.get('cto_user_id'),
            jira_assignee_account_id, jira_reporter_account_id, jira_creator_account_id,
            issue_type, status, issue_priority, story_points, label_list,
            sprint_state, sprint_start_date, sprint_end_date,
            created_ts, updated_ts, issue_resolved_at,
            changed_field, old_value, new_value,
            json.dumps(data)
        )
        
        cur.execute(insert_sql, insert_args)

        if "comment" in event_str:
            comment_data = data.get("comment", {})
            c_id = comment_data.get("id")
            if c_id and issue_id:
                if "deleted" in event_str:
                    cur.execute("DELETE FROM comments WHERE id = %s", (c_id,))
                else:
                    author_id = upsert_user(cur, comment_data.get("author"))
                    cur.execute("""
                        INSERT INTO comments (id, issue_id, author_account_id, body, created, updated)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET body = EXCLUDED.body, updated = EXCLUDED.updated;
                    """, (c_id, issue_id, author_id, comment_data.get("body"), comment_data.get("created"), comment_data.get("updated")))
            msg = f"Handled Comment event for {issue_key}"

        elif "worklog" in event_str:
            wl_data = data.get("worklog", {})
            wl_id   = wl_data.get("id")
            if wl_id and issue_id:
                if "deleted" in event_str:
                    cur.execute("DELETE FROM worklogs WHERE id = %s", (wl_id,))
                else:
                    author_id = upsert_user(cur, wl_data.get("author"))
                    cur.execute("""
                        INSERT INTO worklogs (id, issue_id, author_account_id, time_spent_seconds, started, created)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET time_spent_seconds = EXCLUDED.time_spent_seconds, started = EXCLUDED.started;
                    """, (wl_id, issue_id, author_id, wl_data.get("timeSpentSeconds"), wl_data.get("started"), wl_data.get("created")))
            msg = f"Handled Worklog event for {issue_key}"

        else:
            if action_name == "DELETED":
                if issue_key:
                    cur.execute("DELETE FROM issues WHERE issue_key = %s", (issue_key,))
                    msg = f"Deleted {issue_key}"
                else:
                    msg = "No key to delete"
            else:
                if not issue_id or not issue_key:
                    conn.commit()
                    return jsonify({"status": "partial_ok", "message": "Logged raw data only"}), 200

                p_id = upsert_project(cur, project_obj)
                r_id = upsert_user(cur, reporter_obj)
                a_id = upsert_user(cur, assignee_obj)

                labels = fields.get("labels", [])
                is_client = "client-reported" in [l.lower() for l in labels]
                planned_sp = fields.get("customfield_10016") or story_points
                qa_defects = 1 if "qa-defect" in [l.lower() for l in labels] else 0
                rev_comments = 1 if "review-comment" in [l.lower() for l in labels] else 0

                cur.execute("""
                    INSERT INTO issues (
                        id, issue_key, project_id, summary, description, 
                        issue_type, status, priority, reporter, assignee, 
                        story_points, planned_story_points, is_client_reported,
                        qa_defects_count, review_comments_count,
                        created, updated, due_date
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) 
                    DO UPDATE SET 
                        issue_key    = EXCLUDED.issue_key,
                        project_id   = EXCLUDED.project_id,
                        summary      = EXCLUDED.summary,
                        description  = EXCLUDED.description,
                        issue_type   = EXCLUDED.issue_type,
                        status       = EXCLUDED.status,
                        priority     = EXCLUDED.priority,
                        reporter     = EXCLUDED.reporter,
                        assignee     = EXCLUDED.assignee,
                        story_points = EXCLUDED.story_points,
                        planned_story_points = COALESCE(issues.planned_story_points, EXCLUDED.planned_story_points),
                        is_client_reported   = EXCLUDED.is_client_reported,
                        qa_defects_count     = EXCLUDED.qa_defects_count,
                        review_comments_count = EXCLUDED.review_comments_count,
                        updated      = EXCLUDED.updated,
                        due_date     = EXCLUDED.due_date;
                    """,
                    (
                        issue_id, issue_key, p_id, summary, description,
                        issue_type, status, priority, r_id, a_id,
                        story_points, planned_sp, is_client,
                        qa_defects, rev_comments,
                        created_ts, updated_ts, due_date
                    )
                )

                # -- Write status history if this is an update with changelog --
                changelog = data.get("changelog", {})
                if changelog:
                    for item in changelog.get("items", []):
                        if item.get("field") == "status":
                            changed_by = data.get("user", {}).get("accountId")
                            cur.execute("""
                                INSERT INTO issue_status_history (issue_id, from_status, to_status, changed_by, changed_at)
                                VALUES (%s, %s, %s, %s, NOW());
                            """, (issue_id, item.get("fromString"), item.get("toString"), changed_by))
                            print(f"  [*] Status Change: {item.get('fromString')} -> {item.get('toString')}", flush=True)

                msg = f"Synced {issue_key}"
        
        conn.commit()
        cur.close()
        conn.close()
        print(f"  [OK] DB Sync Success: {msg}", flush=True)
        
    except Exception as e:
        print(f"  ❌ DB Error: {e}", flush=True)
        try: conn.rollback()
        except: pass
        return jsonify({"error": str(e), "status": "db_error"}), 500

    return jsonify({"status": "ok", "event": event_type, "issue": issue_key}), 200


# ---------------------------------------------
# Health Check
# ---------------------------------------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "Jira Webhook Listener"}), 200





if __name__ == "__main__":
    print("\n" + "="*55)
    print("[>>] JIRA WEBHOOK LISTENER IS NOW LIVE (v3.0 — All 12 Metrics)")
    print("="*55)
    print(f"[+] Local Endpoint  : http://localhost:5000/jira/webhook")
    print(f"[*] Config Endpoint : http://localhost:5000/config  (POST to update Jira creds)")
    print(f"[@] Public URL      : (Check your ngrok terminal)")
    print("="*55 + "\n")

    # -- Auto-load Jira credentials from CTO DB on startup --
    load_jira_config_from_cto_db()

    app.run(host="0.0.0.0", port=5000, debug=True)
