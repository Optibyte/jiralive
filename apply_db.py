import psycopg2
import sys

def apply_sql():
    try:
        conn = psycopg2.connect(
            host="localhost",
            port="5432",
            database="ctolocal",
            user="postgres",
            password="yuva@123"
        )
        conn.autocommit = True
        cur = conn.cursor()
        
        print("Reading db_setup.sql...")
        with open(r'd:\jira_live\db_setup.sql', 'r') as f:
            sql = f.read()
        
        print("Applying SQL schema...")
        cur.execute(sql)
        print("✅ Database schema updated successfully!")
        
        # Add a dummy project to prevent initial "no project" errors
        cur.execute("""
            INSERT INTO projects (id, key, name, project_type) 
            VALUES ('10000', 'PROJ', 'Sample Project', 'software')
            ON CONFLICT (id) DO NOTHING;
        """)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error applying SQL: {e}")
        sys.exit(1)

if __name__ == "__main__":
    apply_sql()
