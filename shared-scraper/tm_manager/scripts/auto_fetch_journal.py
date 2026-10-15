import sys
import os
import time

# PostgreSQL 18 sets REQUESTS_CA_BUNDLE to its own cert path which breaks requests.
# Unset it so requests falls back to its bundled certifi certs.
os.environ.pop("REQUESTS_CA_BUNDLE", None)
os.environ.pop("SSL_CERT_FILE", None)

import requests
from bs4 import BeautifulSoup
from pathlib import Path
import logging

# Add tm_manager to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
import db
import scripts.parse_journal as parse_journal

JOURNAL_URL = "https://search.ipindia.gov.in/IPOJournal/Journal/Trademark"
BASE_URL = "https://search.ipindia.gov.in"
JOURNAL_DIR = Path(os.path.join(os.path.dirname(__file__), '..', 'Journal'))
MIN_JOURNALS_REQUIRED = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def fetch_journal_table():
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(JOURNAL_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    table = soup.find('table', {'id': 'Journal'})
    if not table:
        return []
    
    rows = table.find('tbody').find_all('tr')
    journals = []
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 5:
            continue
        sr_no = cols[0].text.strip()
        journal_no = cols[1].text.strip()
        pub_date = cols[2].text.strip()
        
        # Only take actual journal numbers, sometimes IP India puts garbage
        if not journal_no.isdigit():
            continue
            
        # PDFs are downloaded via POST form submissions, not <a> tags
        pdf_links = []
        for form in cols[4].find_all('form'):
            input_tag = form.find('input', {'name': 'FileName'})
            if input_tag and input_tag.get('value'):
                pdf_links.append(input_tag.get('value'))
                
        if journal_no and pdf_links:
            journals.append({
                "journal_no": journal_no,
                "date": pub_date,
                "links": pdf_links
            })
    return journals

def download_pdfs(journal_no, file_names):
    JOURNAL_DIR.mkdir(exist_ok=True)
    downloaded_paths = []
    headers = {"User-Agent": "Mozilla/5.0"}
    download_url = BASE_URL + "/IPOJournal/Journal/ViewJournal"
    
    for i, file_name in enumerate(file_names):
        filename = f"{journal_no}_part{i+1}.pdf"
        filepath = JOURNAL_DIR / filename
        
        if not filepath.exists():
            logging.info(f"Downloading {filename}...")
            resp = requests.post(download_url, data={"FileName": file_name}, headers=headers, stream=True, timeout=120)
            resp.raise_for_status()
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    
        downloaded_paths.append(str(filepath))
        
    return downloaded_paths

def main():
    logging.info("Starting auto_fetch_journal...")
    conn = db.get_connection()
    try:
        # Fetch distinct journal numbers that have a completed job
        existing_rows = conn.execute("SELECT DISTINCT journal_no FROM journal_jobs WHERE status = 'done'").fetchall()
        existing_numbers = [row['journal_no'] for row in existing_rows]
        logging.info(f"Already parsed journals: {existing_numbers}")
        
        journals = fetch_journal_table()
        if not journals:
            logging.info("No journals found on IP India website.")
            return

        # Sort all scraped journals descending (newest first)
        journals.sort(key=lambda x: int(x['journal_no']), reverse=True)
        
        final_to_process = []
        projected_count = len(existing_numbers)
        max_existing = max([int(n) for n in existing_numbers]) if existing_numbers else 0
        
        for j in journals:
            if j['journal_no'] in existing_numbers:
                continue
                
            # Keep if it's a newer journal than what we have OR if we need backfill
            if int(j['journal_no']) > max_existing or projected_count < MIN_JOURNALS_REQUIRED:
                final_to_process.append(j)
                projected_count += 1

        logging.info(f"Journals to process: {[j['journal_no'] for j in final_to_process]}")
        
        for j in final_to_process:
            logging.info(f"Processing Journal No. {j['journal_no']}")
            try:
                pdf_paths = download_pdfs(j['journal_no'], j['links'])
                logging.info(f"Downloaded {len(pdf_paths)} PDFs. Creating background job...")
                job_id = db.create_journal_job(conn, f"{len(pdf_paths)} files auto-fetched")
                logging.info(f"Created job {job_id}. Running parse batch...")
                parse_journal.run_journal_batch_job(
                    pdf_paths, 
                    job_id, 
                    journal_no_override=j['journal_no'], 
                    journal_date_override=j['date']
                )
                logging.info(f"Successfully finished processing Journal {j['journal_no']}.")
                existing_numbers.append(j['journal_no'])
            except Exception as e:
                logging.error(f"Failed to process Journal {j['journal_no']}: {e}")
                # Clean up partial PDFs to allow retry
                for i in range(1, len(j['links']) + 1):
                    p = JOURNAL_DIR / f"{j['journal_no']}_part{i}.pdf"
                    if p.exists():
                        p.unlink()

    finally:
        conn.close()

if __name__ == "__main__":
    main()
