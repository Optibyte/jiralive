from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import json
import os

app = Flask(__name__)
CORS(app) # Allow React frontend to fetch data

# ─────────────────────────────────────────────
# Jira Credentials — loaded from CTO DB or via /config
# NestJS calls POST /config after validating credentials
# ─────────────────────────────────────────────
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
        print(f"\n🔑 [CONFIG] Jira credentials updated → {jira_config['site_url']} ({jira_config['email']})", flush=True)
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

# ─────────────────────────────────────────────
# PostgreSQL connection (Jira metrics DB)
# ─────────────────────────────────────────────
CTO_DB_CONFIG = {
    "host":     os.environ.get("JIRA_DB_HOST",     "localhost"),
    "port":     os.environ.get("JIRA_DB_PORT",     "5432"),
    "database": os.environ.get("JIRA_DB_NAME",     "jira_dashboard"),
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
            print(f"\n✅ [STARTUP] Loaded Jira config from CTO DB → {row[0]} ({row[1]})", flush=True)
        else:
            print("\n⚠️  [STARTUP] No verified Jira credentials found in CTO DB. Use /integrations → Connect Jira.", flush=True)
    except Exception as e:
        print(f"\n⚠️  [STARTUP] Could not load Jira config from CTO DB: {e}", flush=True)


# ─────────────────────────────────────────────
# Helper to Upsert Users & Projects
# ─────────────────────────────────────────────
def upsert_user(cur, user_data):
    if not user_data or not isinstance(user_data, dict): return None
    account_id = user_data.get("accountId")
    if not account_id: return None
    
    cur.execute("""
        INSERT INTO users (account_id, display_name, email, active, avatar_url)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (account_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            email = EXCLUDED.email,
            active = EXCLUDED.active,
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

# ─────────────────────────────────────────────
# Build WHERE clause from request filters
# Supports: ?project=BANK,ALGO  ?assignee=acc-01,acc-02
# ─────────────────────────────────────────────
def build_filters():
    """Parse URL params into SQL WHERE snippets and argument lists."""
    project_param = request.args.get('project', '')   # "BANK" or "BANK,ALGO"
    assignee_param = request.args.get('assignee', '') # "acc-01" or "acc-01,acc-02"

    project_keys = [p.strip().upper() for p in project_param.split(',') if p.strip()]
    assignee_ids = [a.strip() for a in assignee_param.split(',') if a.strip()]

    where_parts = []
    args = []

    if project_keys:
        placeholders = ','.join(['%s'] * len(project_keys))
        where_parts.append(f"""
            project_id IN (
                SELECT id FROM projects WHERE UPPER(key) IN ({placeholders})
            )
        """)
        args.extend(project_keys)

    if assignee_ids:
        ph = ','.join(['%s'] * len(assignee_ids))
        where_parts.append(f"assignee IN ({ph})")
        args.extend(assignee_ids)

    where_sql = "AND " + " AND ".join(where_parts) if where_parts else ""
    return where_sql, args

# ─────────────────────────────────────────────
# Jira Webhook Endpoint
# ─────────────────────────────────────────────
@app.route('/jira/webhook', methods=['POST'])
def jira_webhook():
    # ── 1. Check Payload ────────────────────────
    data = request.get_json(silent=True, force=True)
    
    if data is None:
        raw_body = request.get_data(as_text=True)
        
        if not raw_body:
            content_len = request.headers.get('Content-Length', '0')
            print(f"\n🔍 [DEBUG] Empty Body Received")
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
                print(f"\n⚠️  [WARN] Received Non-JSON Payload. Error: {e}")
                return jsonify({"message": "Expected JSON", "error": str(e)}), 400

    # ── 2. Identify Event Type ────────────────
    event_type = data.get("webhookEvent") or data.get("issue_event_type_name") or data.get("event")
    triggered_by = data.get("triggeredByUser")
    
    if not event_type:
        if triggered_by:
            return jsonify({"status": "meta_received", "triggered_by": triggered_by}), 200
        if "test" in str(data).lower():
            return jsonify({"message": "Test successful"}), 200
        return jsonify({"message": "Payload received but no event type identified"}), 200

    # ── 3. Extract Details ──────────────────────
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

    # ── 4. Log to Terminal ────────
    color_code = "\033[94m"
    event_str  = str(event_type).lower()
    if "created" in event_str:   color_code = "\033[92m"; action_name = "CREATED"
    elif "deleted" in event_str: color_code = "\033[91m"; action_name = "DELETED"
    elif "updated" in event_str: color_code = "\033[94m"; action_name = "UPDATED"
    else:                        action_name = event_type.upper()
    reset_code = "\033[0m"

    print(f"\n{color_code}──────────────────────────────────────────────────{reset_code}", flush=True)
    print(f"{color_code}🔔 JIRA ACTIVITY: {action_name}{reset_code}", flush=True)
    if issue_key:
        print(f"   Issue    : {issue_key}", flush=True)
        print(f"   Summary  : {summary}", flush=True)
        print(f"   Status   : \033[1m{status}\033[0m", flush=True)
    print(f"{color_code}──────────────────────────────────────────────────{reset_code}", flush=True)

    # ── 5. Database Interaction ──
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        
        cur.execute("""
            INSERT INTO jira_webhook_raw (event_type, issue_key, payload)
            VALUES (%s, %s, %s)
        """, (event_type, issue_key, json.dumps(data)))

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

                # ── Write status history if this is an update with changelog ──
                changelog = data.get("changelog", {})
                if changelog:
                    for item in changelog.get("items", []):
                        if item.get("field") == "status":
                            changed_by = data.get("user", {}).get("accountId")
                            cur.execute("""
                                INSERT INTO issue_status_history (issue_id, from_status, to_status, changed_by, changed_at)
                                VALUES (%s, %s, %s, %s, NOW());
                            """, (issue_id, item.get("fromString"), item.get("toString"), changed_by))
                            print(f"  📝 Status Change: {item.get('fromString')} → {item.get('toString')}", flush=True)

                msg = f"Synced {issue_key}"
        
        conn.commit()
        cur.close()
        conn.close()
        print(f"  ✅ DB Sync Success: {msg}", flush=True)
        
    except Exception as e:
        print(f"  ❌ DB Error: {e}", flush=True)
        try: conn.rollback()
        except: pass
        return jsonify({"error": str(e), "status": "db_error"}), 500

    return jsonify({"status": "ok", "event": event_type, "issue": issue_key}), 200


# ─────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "Jira Webhook Listener"}), 200


# ─────────────────────────────────────────────
# Get ALL 12 Metrics for Dashboard
# Supports: ?project=BANK,ALGO  ?assignee=acc-01  ?start=&end=  ?days=N
# ─────────────────────────────────────────────
@app.route('/metrics', methods=['GET'])
def get_metrics():
    from datetime import date, timedelta
    start_str = request.args.get('start')
    end_str   = request.args.get('end')
    days      = request.args.get('days', 30, type=int)

    if start_str and end_str:
        start_date = start_str
        end_date   = end_str
    else:
        end_date   = str(date.today())
        start_date = str(date.today() - timedelta(days=days))

    where_sql, filter_args = build_filters()

    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        date_where = f"DATE(updated) BETWEEN %s AND %s"
        date_args  = [start_date, end_date]

        # ── 1. Sprint Velocity (Done SP) ──────────────────────
        cur.execute(f"""
            SELECT SUM(COALESCE(story_points, 0))
            FROM issues
            WHERE (status ILIKE '%%done%%' OR status ILIKE '%%resolved%%' OR status ILIKE '%%completed%%')
              AND {date_where} {where_sql}
        """, date_args + filter_args)
        velocity = cur.fetchone()[0] or 0

        # ── 2. Planned vs Delivered (Commit Ratio + Done-to-Said) ──
        cur.execute(f"""
            SELECT
                SUM(COALESCE(planned_story_points, story_points, 0)) as planned,
                SUM(CASE WHEN status ILIKE '%%done%%' OR status ILIKE '%%resolved%%' OR status ILIKE '%%completed%%'
                         THEN COALESCE(story_points, 0) ELSE 0 END) as delivered,
                COUNT(*) FILTER (WHERE status ILIKE '%%done%%' OR status ILIKE '%%resolved%%' OR status ILIKE '%%completed%%') as done_count,
                COUNT(*) as total_count
            FROM issues
            WHERE {date_where} {where_sql}
        """, date_args + filter_args)
        res = cur.fetchone()
        planned       = res[0] or 1
        delivered     = res[1] or 0
        done_count    = res[2] or 0
        total_count   = res[3] or 0
        commit_ratio  = round((delivered / planned * 100), 2)
        delivery_commitment = round((done_count / total_count * 100), 2) if total_count > 0 else 0

        # ── 3. Capacity ───────────────────────────────────────────
        cur.execute("SELECT SUM(capacity) FROM sprints")
        total_capacity = cur.fetchone()[0] or 100

        # ── 4. Productivity ───────────────────────────────────────
        productivity = round(delivered / total_capacity, 2)

        # ── 5. Sprint Velocity per Person ─────────────────────────
        cur.execute(f"""
            SELECT COUNT(DISTINCT assignee) FROM issues
            WHERE (status ILIKE '%%done%%' OR status ILIKE '%%resolved%%' OR status ILIKE '%%completed%%')
              AND {date_where} {where_sql}
        """, date_args + filter_args)
        done_assignees = cur.fetchone()[0] or 1
        velocity_per_person = round(velocity / done_assignees, 2) if done_assignees > 0 else 0

        # ── 6. Defect Metrics ─────────────────────────────────────
        cur.execute(f"""
            SELECT 
                COUNT(*) FILTER (WHERE issue_type ILIKE '%%bug%%' OR issue_type ILIKE '%%defect%%') as total_bugs,
                SUM(qa_defects_count) as qa_defects,
                SUM(review_comments_count) as review_comments,
                COUNT(*) FILTER (WHERE is_client_reported = TRUE) as client_defects
            FROM issues
            WHERE {date_where} {where_sql}
        """, date_args + filter_args)
        bug_res = cur.fetchone()
        total_bugs     = bug_res[0] or 0
        qa_defects     = (bug_res[1] or 0) + (bug_res[2] or 0)
        client_defects = bug_res[3] or 0

        defect_density_sp = round((qa_defects / planned * 1000), 2) if planned > 0 else 0
        
        total_valid_defects = qa_defects + client_defects
        defect_leakage = round((client_defects / total_valid_defects * 100), 2) if total_valid_defects > 0 else 0

        # ── 7. Resource Utilization ───────────────────────────────
        cur.execute(f"""
            SELECT SUM(w.time_spent_seconds) FROM worklogs w
            JOIN issues i ON w.issue_id = i.id
            WHERE DATE(w.started) BETWEEN %s AND %s {where_sql}
        """, date_args + filter_args)
        actual_effort_seconds = cur.fetchone()[0] or 0
        actual_effort_hours = actual_effort_seconds / 3600
        capacity_hours = total_capacity * 8
        utilization = round((actual_effort_hours / capacity_hours * 100), 2) if capacity_hours > 0 else 0

        # ── 8. Defect Density (Effort-based) ─────────────────────
        defect_density_effort = round((qa_defects + client_defects) / actual_effort_hours, 4) if actual_effort_hours > 0 else 0

        # ── 9. Defect Reopen Rate (from status history) ───────────
        cur.execute(f"""
            SELECT COUNT(*) FROM issue_status_history sh
            JOIN issues i ON sh.issue_id = i.id
            WHERE (i.issue_type ILIKE '%%bug%%' OR i.issue_type ILIKE '%%defect%%')
              AND sh.from_status IN ('Done', 'Resolved', 'Closed')
              AND sh.to_status IN ('In Progress', 'Reopened', 'Open', 'To Do')
              AND DATE(sh.changed_at) BETWEEN %s AND %s {where_sql.replace('AND project_id', 'AND i.project_id').replace('AND assignee', 'AND i.assignee')}
        """, date_args + filter_args)
        reopen_count = cur.fetchone()[0] or 0
        reopen_rate = round((reopen_count / total_bugs * 100), 2) if total_bugs > 0 else 0

        # ── 10. QA Rejection Rate ─────────────────────────────────
        cur.execute(f"""
            SELECT COUNT(*) FROM issue_status_history sh
            JOIN issues i ON sh.issue_id = i.id
            WHERE (i.issue_type ILIKE '%%bug%%' OR i.issue_type ILIKE '%%defect%%')
              AND (sh.to_status ILIKE '%%reject%%' OR sh.to_status ILIKE '%%won%%t fix%%' OR sh.to_status ILIKE '%%invalid%%')
              AND DATE(sh.changed_at) BETWEEN %s AND %s {where_sql.replace('AND project_id', 'AND i.project_id').replace('AND assignee', 'AND i.assignee')}
        """, date_args + filter_args)
        rejected_count = cur.fetchone()[0] or 0
        qa_rejection_rate = round((rejected_count / total_bugs * 100), 2) if total_bugs > 0 else 0

        # ── 11. Requirements Stability Index (RSI) ────────────────
        # RSI = 1 - (added + removed after sprint start / original planned)
        # Approximated using issue creation dates vs sprint dates
        cur.execute("""
            SELECT 
                COUNT(*) FILTER (WHERE DATE(i.created) > s.start_date) as added_after,
                COUNT(*) as total_sprint_issues
            FROM issues i
            JOIN issue_sprints isp ON i.id = isp.issue_id
            JOIN sprints s ON isp.sprint_id = s.id
            WHERE s.state = 'ACTIVE'
        """)
        rsi_res = cur.fetchone()
        added_after = rsi_res[0] or 0
        total_sprint_issues = rsi_res[1] or 1
        rsi = round((1 - (added_after / total_sprint_issues)) * 100, 2)

        # ── 12. Assignees ─────────────────────────────────────────
        cur.execute(f"""
            SELECT COUNT(DISTINCT assignee) FROM issues
            WHERE {date_where} {where_sql}
        """, date_args + filter_args)
        assignees = cur.fetchone()[0] or 0

        # ── Build Detailed Metrics Array (all 12) ─────────────────
        detailed_metrics = [
            {"type": "Productivity",       "name": "Productivity",                    "uom": "Ratio",           "formula": "Story points delivered / Available Capacity",           "value": productivity,           "target": "> 0.8",  "trend": "up"},
            {"type": "Project Management", "name": "Done to Said Ratio",              "uom": "%",               "formula": "Story points delivered / Story points planned",          "value": f"{commit_ratio}%",    "target": "> 90%",  "trend": "up"},
            {"type": "Productivity",       "name": "Sprint Velocity",                 "uom": "Story Points",    "formula": "Total story points delivered",                          "value": int(delivered),         "target": "> 30",   "trend": "up"},
            {"type": "Productivity",       "name": "Sprint Velocity per Person",      "uom": "SP / Person",     "formula": "Story points delivered / # of contributors",             "value": velocity_per_person,    "target": "> 5",    "trend": "up"},
            {"type": "Quality",            "name": "Defect Density (Story Points)",   "uom": "Defects / SP",    "formula": "(QA defects + review comments) / total story points",    "value": defect_density_sp,     "target": "< 0.5",  "trend": "down"},
            {"type": "Quality",            "name": "Defect Leakage to Client",        "uom": "%",               "formula": "(Client defects / Total defects) × 100",                "value": f"{defect_leakage}%",  "target": "< 5%",   "trend": "down"},
            {"type": "Project Management", "name": "Resource Utilization",            "uom": "%",               "formula": "(Actual effort hours / Capacity hours) × 100",          "value": f"{utilization}%",     "target": "80-90%", "trend": "up"},
            {"type": "Project Management", "name": "Delivery Commitment",             "uom": "%",               "formula": "Delivered stories / Planned stories × 100",             "value": f"{delivery_commitment}%", "target": "> 85%", "trend": "up"},
            {"type": "Quality",            "name": "Defect Density (Efforts)",        "uom": "Defects / Hour",  "formula": "(QA + Client defects) / Effort hours",                  "value": defect_density_effort,  "target": "< 0.1",  "trend": "down"},
            {"type": "Quality",            "name": "Requirements Stability Index",    "uom": "%",               "formula": "(1 - added after sprint start / total planned) × 100",  "value": f"{rsi}%",             "target": "> 85%",  "trend": "up"},
            {"type": "Quality",            "name": "Defect Reopen Rate",              "uom": "%",               "formula": "Reopened defects / QA defects × 100",                   "value": f"{reopen_rate}%",     "target": "< 10%",  "trend": "down"},
            {"type": "Quality",            "name": "QA Defect Rejection Rate",        "uom": "%",               "formula": "Rejected defects / QA defects × 100",                   "value": f"{qa_rejection_rate}%","target": "< 15%",  "trend": "down"},
        ]

        cur.close()
        conn.close()

        return jsonify({
            "velocity":           velocity,
            "commit_ratio":       f"{commit_ratio}%",
            "productivity":       productivity,
            "defect_density":     defect_density_sp,
            "planned":            planned,
            "delivered":          delivered,
            "assignees":          assignees,
            "bugs":               total_bugs,
            "velocity_per_person": velocity_per_person,
            "delivery_commitment": f"{delivery_commitment}%",
            "defect_leakage":     f"{defect_leakage}%",
            "utilization":        f"{utilization}%",
            "rsi":                f"{rsi}%",
            "reopen_rate":        f"{reopen_rate}%",
            "qa_rejection_rate":  f"{qa_rejection_rate}%",
            "detailed_metrics":   detailed_metrics,
            "start_date":         start_date,
            "end_date":           end_date
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# View all stored issues
# ─────────────────────────────────────────────
@app.route('/issues', methods=['GET'])
def get_issues():
    where_sql, filter_args = build_filters()
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(f"""
            SELECT id, issue_key, summary, status, assignee, created, updated, issue_type, story_points 
            FROM issues 
            WHERE 1=1 {where_sql}
            ORDER BY updated DESC
        """, filter_args)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        issues = [
            {
                "id": r[0], "issue_key": r[1], "summary": r[2], "status": r[3],
                "assignee": r[4], "created": str(r[5]), "updated": str(r[6]),
                "issue_type": r[7], "story_points": r[8]
            }
            for r in rows
        ]
        return jsonify(issues), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# Chart Data Endpoint (daily aggregation)
# ─────────────────────────────────────────────
@app.route('/metrics/chart', methods=['GET'])
def get_chart_data():
    from datetime import date, timedelta
    start_str = request.args.get('start')
    end_str   = request.args.get('end')
    days      = request.args.get('days', 30, type=int)

    if start_str and end_str:
        start_date = start_str
        end_date   = end_str
    else:
        end_date   = str(date.today())
        start_date = str(date.today() - timedelta(days=days))

    where_sql, filter_args = build_filters()

    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        cur.execute(f"""
            SELECT 
                DATE(updated) as day,
                COUNT(*) FILTER (WHERE status ILIKE '%%done%%' OR status ILIKE '%%resolved%%' OR status ILIKE '%%completed%%') as done,
                COUNT(*) FILTER (WHERE status ILIKE '%%progress%%' OR status ILIKE '%%doing%%') as in_progress,
                COUNT(*) FILTER (WHERE status ILIKE '%%to do%%' OR status ILIKE '%%backlog%%' OR status ILIKE '%%open%%') as todo,
                SUM(COALESCE(story_points, 0)) as story_points
            FROM issues
            WHERE DATE(updated) BETWEEN %s AND %s {where_sql}
            GROUP BY DATE(updated)
            ORDER BY day ASC
        """, [start_date, end_date] + filter_args)

        rows = cur.fetchall()
        cur.close()
        conn.close()

        data = [
            {
                "date": str(r[0]), "done": r[1], "in_progress": r[2],
                "todo": r[3], "story_points": float(r[4]) if r[4] else 0
            }
            for r in rows
        ]
        return jsonify(data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n" + "="*55)
    print("🚀 JIRA WEBHOOK LISTENER IS NOW LIVE (v3.0 — All 12 Metrics)")
    print("="*55)
    print(f"📍 Local Endpoint  : http://localhost:5000/jira/webhook")
    print(f"📊 Metrics API     : http://localhost:5000/metrics")
    print(f"📈 Charts API      : http://localhost:5000/metrics/chart")
    print(f"🔍 Issues API      : http://localhost:5000/issues")
    print(f"🔑 Config Endpoint : http://localhost:5000/config  (POST to update Jira creds)")
    print(f"🌐 Public URL      : (Check your ngrok terminal)")
    print("="*55 + "\n")

    # ── Auto-load Jira credentials from CTO DB on startup ──
    load_jira_config_from_cto_db()

    app.run(host="0.0.0.0", port=5000, debug=True)
