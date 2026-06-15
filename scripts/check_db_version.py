"""Check PostgreSQL database version."""
import psycopg
from src.config import settings

try:
    # Connect to database
    conn = psycopg.connect(settings.pg_dsn)
    
    with conn.cursor() as cur:
        # Get PostgreSQL version
        cur.execute("SELECT version();")
        pg_version = cur.fetchone()[0]
        print(f"PostgreSQL Version: {pg_version}")
        
        # Get pgvector version
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
        vector_version = cur.fetchone()
        if vector_version:
            print(f"pgvector Version: {vector_version[0]}")
        else:
            print("pgvector: Not installed")
        
        # Get database size
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()));")
        db_size = cur.fetchone()[0]
        print(f"Database Size: {db_size}")
        
        # Get table count
        cur.execute("""
            SELECT COUNT(*) 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
        """)
        table_count = cur.fetchone()[0]
        print(f"Tables: {table_count}")
    
    conn.close()
    
except Exception as e:
    print(f"Error: {e}")
    print("\nTroubleshooting:")
    print("1. Make sure PostgreSQL is running")
    print("2. Check credentials in .env file")
    print("3. Verify pgvector extension is installed")
