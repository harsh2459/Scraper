import psycopg
import sys
import os

# Add tm_manager dir to path to import config/db
sys.path.append(os.path.join(os.path.dirname(__file__), 'tm_manager'))
import db

def delete_journal(journal_no):
    conn = db.get_connection()
    try:
        # Delete matches and entries
        conn.execute("DELETE FROM journal_entries WHERE journal_no = %s", (journal_no,))
        
        # Delete jobs
        conn.execute("DELETE FROM journal_jobs WHERE journal_no = %s", (journal_no,))
        
        conn.commit()
        print(f"Successfully deleted journal {journal_no} from database.")
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    delete_journal("2274")
