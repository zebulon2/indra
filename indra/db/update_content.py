import os
from io import StringIO
import csv
import shutil
import tarfile
import tempfile
from ftplib import FTP
from collections import namedtuple
from indra import db
import functools
import multiprocessing as mp
from indra.util import write_unicode_csv

pmc_ftp_url = 'ftp.ncbi.nlm.nih.gov'
blocksize=33554432 # Chunk size recommended by NCBI

PmcInfo = namedtuple('PmcInfo', ('File', 'PMCID', 'PMID', 'MID'))

def initialize_pmc_manuscripts():
    auth_dir = '/pub/pmc/manuscript'
    # FIXME
    #tmp_dir = tempfile.mkdtemp(prefix='tmpIndra', dir='.')
    tmp_dir = 'tmpIndra49g0_5j1'

    # Get an FTP connection
    ftp = FTP(pmc_ftp_url)
    ftp.login()
    # Change to the manuscripts directory
    ftp.cwd(auth_dir)

    def get_file_info():
        # Get the list of files from the CSV file
        filelist_bytes = []
        print("Downloading filelist.csv")
        ftp.retrbinary('RETR filelist.csv',
                      callback=lambda b: filelist_bytes.append(b),
                      blocksize=blocksize)
        filelist_csv = b''.join(filelist_bytes).decode('ascii').split('\n')
        # Namedtuple for working with PMC info entries
        print("Processing filelist.csv")
        # Process the file info (skip the header line)
        pmc_info_list = [PmcInfo(*line.split(',')) for line in filelist_csv
                         if line][1:]
        return pmc_info_list

    def update_text_refs(pmc_info_list):
        """Insert any missing text_refs into database."""
        stored_pmc_ids = [r[0] for r in db.select('text_ref', 'pmcid')]
        missing_set = set([p.PMCID for p in pmc_info_list]).difference(
                                                          set(stored_pmc_ids))
        pmc_info_missing = [p for p in pmc_info_list if p.PMCID in missing_set]
        # Write the missing records to a CSV so we can use the copy command
        pmc_info_to_copy = []
        for pi in pmc_info_missing:
            if pi.PMID == '0':
                pmid = '\\N'
            else:
                pmid = pi.PMID
            pmc_info_to_copy.append(('pmc', pmid, pi.PMCID, pi.MID))
        missing_mids_csv = os.path.join(tmp_dir, 'missing_mids.tsv')
        write_unicode_csv(missing_mids_csv, pmc_info_to_copy, delimiter='\t')
        conn = db.get_connection()
        cur = conn.cursor()
        with open(missing_mids_csv, 'rt') as f:
            cur.copy_from(f, 'text_ref', size=1000000,
                          columns=('source', 'pmid', 'pmcid', 'manuscript_id'))
        conn.commit()

    def get_xml_archive_list():
        # Get the list of .xml.tar.gz files
        xml_files = [f[0] for f in ftp.mlsd() if f[0].endswith('.xml.tar.gz')]
        print("xml_files: %s" % xml_files)
        return xml_files

    def download_xml_archive(filename):
        # Some variables for meaningful progress messages
        stored_bytes = 0
        pcts_to_log = list(range(0, 101, 5))
        #loaded_pmc_ids = db.get_auth_xml_pmcids()
        # FIXME this could be eliminated if logging not needed
        # Function to write to local file with progress updates
        def write_to_file(fp, b, total_size):
            nonlocal stored_bytes, pcts_to_log
            fp.write(b)
            stored_bytes += len(b)
            pct_complete = round(100 * (stored_bytes / float(total_size)))
            if pct_complete in pcts_to_log:
                print('%s: %s%% complete' % (filename, pct_complete))
                pcts_to_log.remove(pct_complete)
        outfilepath = os.path.join(tmp_dir, filename)
        filesize = ftp.size(filename)
        print("Getting %s" % filename)
        with open(outfilepath, 'wb') as f:
            ftp.retrbinary('RETR %s' % filename,
                           callback=lambda b: write_to_file(f, b, filesize),
                           blocksize=blocksize)
        ftp.close()
        # Extract all files in the TAR archive
        tf = tarfile.open(outfilepath)
        print("Extracting all files from %s" % outfilepath)
        tf.extractall(path=tmp_dir)

    def update_text_content(pmc_info_list):
        # Get list of PMCIDs for which we've already stored author manuscripts
        stored_pmc_ids = db.get_auth_xml_pmcids()
        print("%d pmc_auth_xml PMCIDs already in DB" % len(stored_pmc_ids))
        missing_set = set([p.PMCID for p in pmc_info_list]).difference(
                                                          set(stored_pmc_ids))
        pmc_info_missing = [p for p in pmc_info_list if p.PMCID in missing_set]
        print("%d pmc_auth_xml PMCIDs left to load in DB" %
              len(pmc_info_missing))
        # Get the text ref IDs by PMCID
        pmcid_tr_dict = dict(db.get_text_refs_by_pmcid(
                                 tuple([pi.PMCID for pi in pmc_info_missing])))
        content_block_rows = []
        blocksize = 2000
        for pi in pmc_info_missing[0:blocksize]:
            xml_path = os.path.join(tmp_dir, pi.File)
            if os.path.exists(xml_path):
                # Look up the text_ref_id for this PMCID
                text_ref_id = pmcid_tr_dict[pi.PMCID]
                # Read the XML file in text mode
                with open(xml_path, 'rt') as f:
                    content = f.read()
                # Add to our CSV rows
                content_block_rows.append([text_ref_id,
                                           'pmc_auth_xml', content])
            else:
                print("Could not find file %s" % xml_path)
        # Write the content data to a StringIO in CSV format
        content_block_csv = StringIO()
        writer = csv.writer(content_block_csv, delimiter=',', quotechar='"',
                            quoting=csv.QUOTE_ALL)
        writer.writerows(content_block_rows)
        # Copy the data in CSV format to the Postgres DB
        conn = db.get_connection()
        cur = conn.cursor()
        sql =  """COPY text_content (text_ref_id, content_type, content)
                    FROM STDIN WITH (FORMAT csv);"""
        cur.copy_expert(sql, content_block_csv, size=1000000)
        conn.commit()
        content_block_csv.close() # Close the StringIO

    # The high-level procedure:
    pmc_info_list = get_file_info()
    update_text_refs(pmc_info_list)
    xml_files = get_xml_archive_list()
    filename = 'PMC002XXXXXX.xml.tar.gz'
    update_text_content(pmc_info_list)


if __name__ == '__main__':
    #db.drop_tables()
    #db.create_tables()
    initialize_pmc_manuscripts()
    db.insert_reach('3', '1.3.3', "{'foo': 'bar'}")


"""
tmp_dir = 'tmpIndraipij964n'
filepath = 'tmpIndraipij964n/PMC002XXXXXX.xml.tar.gz'
tf = tarfile.open(name=filepath)
tf.extract(

# Select full list of PMCIDs from database
# Get full list of papers from oa_file_list.csv
# Identify intersection
# Get XMLs for remaining set

# For each file in the list, check the database, and if it's in the DB already
# skip; for the others, obtain.
"""
