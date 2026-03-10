"""
Jira Webhook Listener - V3 (Write-Only)
PURPOSE: Listen for Jira webhook events, extract all IDs, resolve CTO mappings, store enriched raw events.
NO metrics endpoints - those are handled by NestJS directly querying the database.
"""

import json
import os
from datetime import datetime
from uuid import uuid4
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__)
CORS(app)

# Database connection
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "database": os.getenv("DB_NAME", "jira_dashboard"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "yuva@123")
}

def get_db_connection():
    """Get database connection with proper settings"""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn

def extract_from_payload(payload):
    """Extract all relevant IDs and attributes from Jira webhook payload"""
    try:
        issue = payload.get('issue', {})
        fields = issue.get('fields', {})
        
        # Jira-side identifiers
        jira_issue_id = issue.get('id')
        jira_issue_key = issue.get('key')
        jira_project_id = fields.get('project', {}).get('id')
        jira_project_key = fields.get('project', {}).get('key')
        jira_board_id = payload.get('board', {}).get('id')  # From webhook context
        
        # Sprint info
        sprint_data = {}
        sprint_list = fields.get('sprint', [])
        if sprint_list:
            if isinstance(sprint_list, list) and len(sprint_list) > 0:
                sprint_data = sprint_list[0]
            elif isinstance(sprint_list, dict):
                sprint_data = sprint_list
        
        jira_sprint_id = sprint_data.get('id') if sprint_data else None
        jira_sprint_name = sprint_data.get('name') if sprint_data else None
        
        # Assignee and user details
        assignee = fields.get('assignee') or {}
        jira_assignee_account_id = assignee.get('accountId')
        jira_reporter_account_id = fields.get('reporter', {}).get('accountId')
        jira_creator_account_id = fields.get('creator', {}).get('accountId')
        
        # Issue attributes
        issue_type = fields.get('issuetype', {}).get('name')
        issue_status = fields.get('status', {}).get('name')
        issue_priority = fields.get('priority', {}).get('name')
        story_points = fields.get('customfield_10016')  # Typical story points field ID
        labels = fields.get('labels', [])
        
        # Timestamps
        issue_created_at = fields.get('created')
        issue_updated_at = fields.get('updated')
        issue_resolved_at = fields.get('resolutiondate')
        
        # Change tracking (from changelog if available)
        change_log = payload.get('changelog', {})
        histories = change_log.get('histories', [])
        changed_field = None
        old_value = None
        new_value = None
        
        if histories:
            last_change = histories[-1]
            items = last_change.get('items', [])
            if items:
                item = items[0]
                changed_field = item.get('field')
                old_value = item.get('fromString')
                new_value = item.get('toString')
        
        return {
            'jira_issue_id': jira_issue_id,
            'jira_issue_key': jira_issue_key,
            'jira_project_id': jira_project_id,
            'jira_project_key': jira_project_key,
            'jira_board_id': jira_board_id,
            'jira_sprint_id': jira_sprint_id,
            'jira_sprint_name': jira_sprint_name,
            'jira_assignee_account_id': jira_assignee_account_id,
            'jira_reporter_account_id': jira_reporter_account_id,
            'jira_creator_account_id': jira_creator_account_id,
            'issue_type': issue_type,
            'issue_status': issue_status,
            'issue_priority': issue_priority,
            'story_points': float(story_points) if story_points else None,
            'labels': labels,
            'issue_created_at': issue_created_at,
            'issue_updated_at': issue_updated_at,
            'issue_resolved_at': issue_resolved_at,
            'changed_field': changed_field,
            'old_value': old_value,
            'new_value': new_value,
        }
    except Exception as e:
        logger.error(f"Error extracting payload: {e}")
        return {}

def resolve_cto_ids(jira_project_key, jira_assignee_account_id, conn):
    """
    Resolve CTO platform IDs from mapping table.
    REQUIRES: jira_integrations table in the CTO database (ctolocal)
    For now, if CTO DB is separate, this returns empty. When integrated, queries CTO DB.
    """
    
    cto_ids = {
        'cto_project_id': None,
        'cto_account_id': None,
        'cto_market_id': None,
        'cto_org_id': None,
        'cto_team_id': None,
        'cto_user_id': None,
    }
    
    try:
        # If CTO database is available, query jira_integrations
        # For MVP, this is skipped. In production, requires:
        # - Separate CTO PostgreSQL connection
        # - jira_integrations table with mappings
        # Example query:
        # SELECT p.id as cto_project_id, p.account_id, a.market_id, a.org_id, u.team_id, u.id
        # FROM jira_integrations ji
        # JOIN projects p ON p.id = ji.project_id
        # JOIN accounts a ON a.id = p.account_id
        # LEFT JOIN users u ON u.jira_account_id = jira_assignee_account_id
        # WHERE ji.jira_project_key = jira_project_key
        
        logger.info(f"CTO ID resolution would map Jira project {jira_project_key} to CTO org")
        
    except Exception as e:
        logger.error(f"Error resolving CTO IDs: {e}")
    
    return cto_ids

def insert_raw_event(payload, extracted_data, conn):
    """Insert enriched raw event into jira_webhook_raw table"""
    
    cur = conn.cursor()
    
    try:
        # Build event row
        event_row = {
            'event_id': payload.get('id', str(uuid4())),
            'event_type': payload.get('webhookEvent'),
            'received_at': 'NOW()',  # SQL function
            'jira_issue_id': extracted_data.get('jira_issue_id'),
            'jira_issue_key': extracted_data.get('jira_issue_key'),
            'jira_project_id': extracted_data.get('jira_project_id'),
            'jira_project_key': extracted_data.get('jira_project_key'),
            'jira_board_id': extracted_data.get('jira_board_id'),
            'jira_sprint_id': extracted_data.get('jira_sprint_id'),
            'jira_sprint_name': extracted_data.get('jira_sprint_name'),
            'jira_assignee_account_id': extracted_data.get('jira_assignee_account_id'),
            'jira_reporter_account_id': extracted_data.get('jira_reporter_account_id'),
            'jira_creator_account_id': extracted_data.get('jira_creator_account_id'),
            'issue_type': extracted_data.get('issue_type'),
            'issue_status': extracted_data.get('issue_status'),
            'issue_priority': extracted_data.get('issue_priority'),
            'story_points': extracted_data.get('story_points'),
            'labels': extracted_data.get('labels'),
            'issue_created_at': extracted_data.get('issue_created_at'),
            'issue_updated_at': extracted_data.get('issue_updated_at'),
            'issue_resolved_at': extracted_data.get('issue_resolved_at'),
            'changed_field': extracted_data.get('changed_field'),
            'old_value': extracted_data.get('old_value'),
            'new_value': extracted_data.get('new_value'),
            'raw_payload': json.dumps(payload),
            'processing_status': 'stored'
        }
        
        # Resolve CTO IDs
        cto_ids = resolve_cto_ids(
            event_row['jira_project_key'],
            event_row['jira_assignee_account_id'],
            conn
        )
        event_row.update(cto_ids)
        
        # Build SQL
        cols = ', '.join(event_row.keys())
        placeholders = ', '.join(['%s'] * len(event_row))
        sql = f"""
            INSERT INTO jira_webhook_raw ({cols})
            VALUES ({placeholders})
            ON CONFLICT (event_id) DO NOTHING
        """
        
        # Convert NOW() function - don't use placeholder for it
        values = []
        for key in event_row.keys():
            val = event_row[key]
            if key == 'received_at' and val == 'NOW()':
                # Handle separately in SQL
                continue
            values.append(val)
        
        # Actually, use DEFAULT for received_at
        sql = f"""
            INSERT INTO jira_webhook_raw (
                event_id, event_type, jira_issue_id, jira_issue_key, jira_project_id,
                jira_project_key, jira_board_id, jira_sprint_id, jira_sprint_name,
                jira_assignee_account_id, jira_reporter_account_id, jira_creator_account_id,
                issue_type, issue_status, issue_priority, story_points, labels,
                issue_created_at, issue_updated_at, issue_resolved_at,
                changed_field, old_value, new_value,
                cto_project_id, cto_account_id, cto_market_id, cto_org_id, cto_team_id, cto_user_id,
                raw_payload, processing_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
        """
        
        vals = (
            event_row['event_id'],
            event_row['event_type'],
            event_row['jira_issue_id'],
            event_row['jira_issue_key'],
            event_row['jira_project_id'],
            event_row['jira_project_key'],
            event_row['jira_board_id'],
            event_row['jira_sprint_id'],
            event_row['jira_sprint_name'],
            event_row['jira_assignee_account_id'],
            event_row['jira_reporter_account_id'],
            event_row['jira_creator_account_id'],
            event_row['issue_type'],
            event_row['issue_status'],
            event_row['issue_priority'],
            event_row['story_points'],
            event_row['labels'],
            event_row['issue_created_at'],
            event_row['issue_updated_at'],
            event_row['issue_resolved_at'],
            event_row['changed_field'],
            event_row['old_value'],
            event_row['new_value'],
            event_row['cto_project_id'],
            event_row['cto_account_id'],
            event_row['cto_market_id'],
            event_row['cto_org_id'],
            event_row['cto_team_id'],
            event_row['cto_user_id'],
            event_row['raw_payload'],
            event_row['processing_status']
        )
        
        cur.execute(sql, vals)
        logger.info(f"Stored raw event: {event_row['event_id']} ({event_row['event_type']})")
        
    except Exception as e:
        logger.error(f"Error inserting raw event: {e}")
        raise
    finally:
        cur.close()

def upsert_normalized_tables(payload, extracted_data, cto_ids, conn):
    """
    Also upsert to normalized issues, projects, users tables for relational queries.
    This maintains backward compatibility.
    """
    cur = conn.cursor()
    
    try:
        # Upsert project
        if extracted_data.get('jira_project_id'):
            cur.execute("""
                INSERT INTO projects (id, key, name, project_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    key = EXCLUDED.key,
                    name = EXCLUDED.name,
                    project_type = EXCLUDED.project_type
            """, (
                extracted_data['jira_project_id'],
                extracted_data['jira_project_key'],
                payload.get('issue', {}).get('fields', {}).get('project', {}).get('name'),
                'software'
            ))
        
        # Upsert users (assignee, reporter, creator)
        for account_id in [
            extracted_data.get('jira_assignee_account_id'),
            extracted_data.get('jira_reporter_account_id'),
            extracted_data.get('jira_creator_account_id')
        ]:
            if account_id:
                user_data = None
                for u in [payload['issue']['fields'].get('assignee'),
                         payload['issue']['fields'].get('reporter'),
                         payload['issue']['fields'].get('creator')]:
                    if u and u.get('accountId') == account_id:
                        user_data = u
                        break
                
                if user_data:
                    cur.execute("""
                        INSERT INTO users (account_id, display_name, email, active)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (account_id) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            email = EXCLUDED.email,
                            active = EXCLUDED.active
                    """, (
                        account_id,
                        user_data.get('displayName'),
                        user_data.get('emailAddress'),
                        True
                    ))
        
        # Upsert issue
        if extracted_data.get('jira_issue_id'):
            cur.execute("""
                INSERT INTO issues (
                    id, issue_key, project_id, summary, description,
                    issue_type, status, priority, assignee, reporter,
                    story_points, created, updated
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    assignee = EXCLUDED.assignee,
                    story_points = EXCLUDED.story_points,
                    updated = EXCLUDED.updated
            """, (
                extracted_data['jira_issue_id'],
                extracted_data['jira_issue_key'],
                extracted_data['jira_project_id'],
                payload['issue']['fields'].get('summary'),
                payload['issue']['fields'].get('description'),
                extracted_data.get('issue_type'),
                extracted_data.get('issue_status'),
                extracted_data.get('issue_priority'),
                extracted_data.get('jira_assignee_account_id'),
                extracted_data.get('jira_reporter_account_id'),
                extracted_data.get('story_points'),
                extracted_data.get('issue_created_at'),
                extracted_data.get('issue_updated_at')
            ))
        
        logger.info("Normalized tables updated")
        
    except Exception as e:
        logger.error(f"Error upserting normalized tables: {e}")
        # Don't raise - normalized upsert is optional
    finally:
        cur.close()


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'OK', 'service': 'Jira Webhook Listener V3'}), 200

@app.route('/jira/webhook', methods=['POST'])
def handle_webhook():
    """
    Main webhook handler - Receives Jira events, extracts all IDs, stores in enriched raw table.
    This is the ONLY data-writing endpoint.
    """
    
    try:
        payload = request.get_json()
        
        if not payload:
            return jsonify({'error': 'Empty payload'}), 400
        
        # Extract all data from payload
        extracted_data = extract_from_payload(payload)
        
        # Get DB connection
        conn = get_db_connection()
        
        # Insert into enriched raw table
        insert_raw_event(payload, extracted_data, conn)
        
        # Also upsert normalized tables for backward compat
        cto_ids = {}  # From resolve, to be passed along
        upsert_normalized_tables(payload, extracted_data, cto_ids, conn)
        
        conn.close()
        
        return jsonify({
            'status': 'stored',
            'event_id': extracted_data.get('jira_issue_key'),
            'event_type': payload.get('webhookEvent')
        }), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/config', methods=['POST'])
def update_config():
    """
    Receive Jira credential updates from NestJS.
    In production, this would update jira_integrations mappings.
    """
    try:
        data = request.get_json()
        
        # TODO: Validate and store Jira credentials
        # For now, just acknowledge
        
        logger.info(f"Config update received: {data.get('jira_site')}")
        return jsonify({'status': 'config_updated'}), 200
        
    except Exception as e:
        logger.error(f"Config error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# REMOVED ENDPOINTS (Migrated to NestJS)
# ============================================================================
# The following endpoints are NO LONGER available in Python.
# They have been migrated to NestJS, which queries the database directly:
#
# REMOVED: GET /metrics          -> NestJS GET /api/v1/jira-metrics
# REMOVED: GET /metrics/chart    -> NestJS GET /api/v1/jira-metrics/chart
# REMOVED: GET /issues           -> NestJS GET /api/v1/jira-metrics/issues
#
# All metrics calculations are now done by NestJS using Prisma queries.
# All RBAC filtering is now done by NestJS based on user role.


if __name__ == '__main__':
    logger.info("Starting Jira Webhook Listener V3 (Write-Only)")
    app.run(host='0.0.0.0', port=5000, debug=False)
