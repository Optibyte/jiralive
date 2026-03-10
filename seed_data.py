import psycopg2
from datetime import datetime, timedelta

def seed_data():
    try:
        conn = psycopg2.connect(
            host="localhost",
            port="5432",
            database="jira_dashboard",
            user="postgres",
            password="yuva@123"
        )
        cur = conn.cursor()
        
        # 1. Users
        users = [
            ('acc-01', 'Alice dev', 'alice@company.com', True),
            ('acc-02', 'Bob dev', 'bob@company.com', True),
            ('acc-03', 'Charlie QA', 'charlie@company.com', True)
        ]
        cur.executemany("INSERT INTO users (account_id, display_name, email, active) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", users)

        # 2. Project
        cur.execute("INSERT INTO projects (id, key, name, project_type) VALUES ('10001', 'ALGO', 'Algorithm Engine', 'software') ON CONFLICT DO NOTHING")

        # 3. Sprints
        sprints = [
            ('spr-01', 'Sprint 1', 'CLOSED', datetime.now()-timedelta(days=30), datetime.now()-timedelta(days=15), 'b-01', 50),
            ('spr-02', 'Sprint 2', 'ACTIVE', datetime.now()-timedelta(days=14), datetime.now()+timedelta(days=1), 'b-01', 60)
        ]
        cur.executemany("INSERT INTO sprints (id, name, state, start_date, end_date, board_id, capacity) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", sprints)

        # 4. Issues
        # Mix of Done, In Progress, Bugs, with labels for QA defects and client reporting
        issues = [
            ('iss-01', 'ALGO-1', '10001', 'Setup DB', 'Story', 'Done', 'High', 'acc-01', 5, 5, False, 0, 1),
            ('iss-02', 'ALGO-2', '10001', 'Implement API', 'Story', 'Done', 'High', 'acc-02', 8, 8, False, 0, 0),
            ('iss-03', 'ALGO-3', '10001', 'Frontend Layout', 'Story', 'Done', 'Medium', 'acc-01', 3, 5, False, 1, 2),
            ('iss-04', 'ALGO-4', '10001', 'Bug in Login', 'Bug', 'Done', 'Highest', 'acc-03', 0, 0, True, 1, 0), # Client reported bug
            ('iss-05', 'ALGO-5', '10001', 'New Dashboard', 'Story', 'In Progress', 'High', 'acc-02', 13, 13, False, 2, 5),
            ('iss-06', 'ALGO-6', '10001', 'Unit Tests', 'Task', 'To Do', 'Low', 'acc-01', 2, 2, False, 0, 0),
        ]
        
        for iss in issues:
            cur.execute("""
                INSERT INTO issues (id, issue_key, project_id, summary, issue_type, status, priority, assignee, story_points, planned_story_points, is_client_reported, qa_defects_count, review_comments_count, updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, updated = NOW()
            """, iss)
            # Map issues to active sprint
            cur.execute("INSERT INTO issue_sprints (issue_id, sprint_id) VALUES (%s, 'spr-02') ON CONFLICT DO NOTHING", (iss[0],))

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Seed data inserted! Dashboard should now show metrics.")
    except Exception as e:
        print(f"❌ Error seeding: {e}")

if __name__ == "__main__":
    seed_data()
