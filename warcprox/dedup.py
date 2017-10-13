'''
warcprox/dedup.py - identical payload digest deduplication using sqlite db

Copyright (C) 2013-2017 Internet Archive

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
USA.
'''

from __future__ import absolute_import

from datetime import datetime
import logging
import os
import json
from hanzo import warctools
import warcprox
import sqlite3
import urllib3

urllib3.disable_warnings()

class DedupDb(object):
    logger = logging.getLogger("warcprox.dedup.DedupDb")

    def __init__(
            self, file='./warcprox.sqlite', options=warcprox.Options()):
        self.file = file
        self.options = options

    def start(self):
        if os.path.exists(self.file):
            self.logger.info(
                    'opening existing deduplication database %s',
                    self.file)
        else:
            self.logger.info(
                    'creating new deduplication database %s', self.file)

        conn = sqlite3.connect(self.file)
        conn.execute(
                'create table if not exists dedup ('
                '  key varchar(300) primary key,'
                '  value varchar(4000)'
                ');')
        conn.commit()
        conn.close()

    def save(self, digest_key, response_record, bucket=""):
        record_id = response_record.get_header(warctools.WarcRecord.ID).decode('latin1')
        url = response_record.get_header(warctools.WarcRecord.URL).decode('latin1')
        date = response_record.get_header(warctools.WarcRecord.DATE).decode('latin1')

        key = digest_key.decode('utf-8') + "|" + bucket

        py_value = {'id':record_id, 'url':url, 'date':date}
        json_value = json.dumps(py_value, separators=(',',':'))

        conn = sqlite3.connect(self.file)
        conn.execute(
                'insert or replace into dedup (key, value) values (?, ?)',
                (key, json_value))
        conn.commit()
        conn.close()
        self.logger.debug('dedup db saved %s:%s', key, json_value)

    def lookup(self, digest_key, bucket=""):
        result = None
        key = digest_key.decode('utf-8') + '|' + bucket
        conn = sqlite3.connect(self.file)
        cursor = conn.execute('select value from dedup where key = ?', (key,))
        result_tuple = cursor.fetchone()
        conn.close()
        if result_tuple:
            result = json.loads(result_tuple[0])
            result['id'] = result['id'].encode('latin1')
            result['url'] = result['url'].encode('latin1')
            result['date'] = result['date'].encode('latin1')
        self.logger.debug('dedup db lookup of key=%s returning %s', key, result)
        return result

    def notify(self, recorded_url, records):
        if (records[0].get_header(warctools.WarcRecord.TYPE) == warctools.WarcRecord.RESPONSE
                and recorded_url.response_recorder.payload_size() > 0):
            digest_key = warcprox.digest_str(
                    recorded_url.response_recorder.payload_digest,
                    self.options.base32)
            if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
                self.save(
                        digest_key, records[0],
                        bucket=recorded_url.warcprox_meta["captures-bucket"])
            else:
                self.save(digest_key, records[0])


def decorate_with_dedup_info(dedup_db, recorded_url, base32=False):
    if (recorded_url.response_recorder
            and recorded_url.response_recorder.payload_digest
            and recorded_url.response_recorder.payload_size() > 0):
        digest_key = warcprox.digest_str(recorded_url.response_recorder.payload_digest, base32)
        if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
            if isinstance(dedup_db, CdxServerDedup):
                recorded_url.dedup_info = dedup_db.lookup(digest_key, recorded_url.warcprox_meta["captures-bucket"],
                                                          recorded_url)
            else:
                recorded_url.dedup_info = dedup_db.lookup(digest_key, recorded_url.warcprox_meta["captures-bucket"])
        else:
            if isinstance(dedup_db, CdxServerDedup):
                recorded_url.dedup_info = dedup_db.lookup(digest_key, recorded_url)
            else:
                recorded_url.dedup_info = dedup_db.lookup(digest_key)

class RethinkDedupDb:
    logger = logging.getLogger("warcprox.dedup.RethinkDedupDb")

    def __init__(self, rr, table="dedup", shards=None, replicas=None, options=warcprox.Options()):
        self.rr = rr
        self.table = table
        self.shards = shards or len(rr.servers)
        self.replicas = replicas or min(3, len(rr.servers))
        self._ensure_db_table()
        self.options = options

    def _ensure_db_table(self):
        dbs = self.rr.db_list().run()
        if not self.rr.dbname in dbs:
            self.logger.info("creating rethinkdb database %r", self.rr.dbname)
            self.rr.db_create(self.rr.dbname).run()
        tables = self.rr.table_list().run()
        if not self.table in tables:
            self.logger.info(
                    "creating rethinkdb table %r in database %r shards=%r "
                    "replicas=%r", self.table, self.rr.dbname, self.shards,
                    self.replicas)
            self.rr.table_create(
                    self.table, primary_key="key", shards=self.shards,
                    replicas=self.replicas).run()


    def start(self):
        pass

    def save(self, digest_key, response_record, bucket=""):
        k = digest_key.decode("utf-8") if isinstance(digest_key, bytes) else digest_key
        k = "{}|{}".format(k, bucket)
        record_id = response_record.get_header(warctools.WarcRecord.ID).decode('latin1')
        url = response_record.get_header(warctools.WarcRecord.URL).decode('latin1')
        date = response_record.get_header(warctools.WarcRecord.DATE).decode('latin1')
        record = {'key':k,'url':url,'date':date,'id':record_id}
        result = self.rr.table(self.table).insert(
                record, conflict="replace").run()
        if sorted(result.values()) != [0,0,0,0,0,1] and [result["deleted"],result["skipped"],result["errors"]] != [0,0,0]:
            raise Exception("unexpected result %s saving %s", result, record)
        self.logger.debug('dedup db saved %s:%s', k, record)

    def lookup(self, digest_key, bucket=""):
        k = digest_key.decode("utf-8") if isinstance(digest_key, bytes) else digest_key
        k = "{}|{}".format(k, bucket)
        result = self.rr.table(self.table).get(k).run()
        if result:
            for x in result:
                result[x] = result[x].encode("utf-8")
        self.logger.debug('dedup db lookup of key=%s returning %s', k, result)
        return result

    def notify(self, recorded_url, records):
        if (records[0].get_header(warctools.WarcRecord.TYPE) == warctools.WarcRecord.RESPONSE
                and recorded_url.response_recorder.payload_size() > 0):
            digest_key = warcprox.digest_str(recorded_url.response_recorder.payload_digest,
                    self.options.base32)
            if recorded_url.warcprox_meta and "captures-bucket" in recorded_url.warcprox_meta:
                self.save(digest_key, records[0], bucket=recorded_url.warcprox_meta["captures-bucket"])
            else:
                self.save(digest_key, records[0])


def _split_timestamp(timestamp):
    """split `timestamp` into a tuple of 6 integers.

    :param timestamp: full-length timestamp.
    :type timestamp: bytes
    """
    return (
        int(timestamp[:-10]),
        int(timestamp[-10:-8]),
        int(timestamp[-8:-6]),
        int(timestamp[-6:-4]),
        int(timestamp[-4:-2]),
        int(timestamp[-2:])
        )


class CdxServerDedup(object):
    """Query a CDX server to perform deduplication.
    """
    logger = logging.getLogger("warcprox.dedup.CdxServerDedup")

    def __init__(self, cdx_url="https://web.archive.org/cdx/search/cdx",
                 options=warcprox.Options()):
        self.http_pool = urllib3.PoolManager()
        self.cdx_url = cdx_url
        self.options = options

    def start(self):
        pass

    def save(self, digest_key, response_record, bucket=""):
        """Does not apply to CDX server, as it is obviously read-only.
        """
        pass

    def lookup(self, digest_key, recorded_url):
        """Compare `sha1` with SHA1 hash of fetched content (note SHA1 must be
        computed on the original content, after decoding Content-Encoding and
        Transfer-Encoding, if any), if they match, write a revisit record.

        :param digest_key: b'sha1:<KEY-VALUE>'.
            Example: b'sha1:B2LTWWPUOYAH7UIPQ7ZUPQ4VMBSVC36A'
        :param recorded_url: RecordedUrl object
        Result must contain:
        {"url", "date": "%Y-%m-%dT%H:%M:%SZ", "id": "warc_id" if available}
        """
        url = recorded_url.url
        u = url.decode("utf-8") if isinstance(url, bytes) else url
        try:
            result = self.http_pool.request('GET', self.cdx_url, fields=dict(
                url=u, fl="timestamp,digest", limit=-1))
        except urllib3.HTTPError as exc:
            self.logger.error('CdxServerDedup request failed for url=%s %s',
                              url, exc)
        if result.status == 200:
            digest_key = digest_key[5:]  # drop sha1: prefix
            for line in result.data.split(b'\n'):
                if line:
                    (cdx_ts, cdx_digest) = line.split(b' ')
                    if cdx_digest == digest_key:
                        dt = datetime(*_split_timestamp(cdx_ts.decode('ascii')))
                        # TODO find out id
                        return dict(id=url, url=url,
                                    date=dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
        return None

    def notify(self, recorded_url, records):
        """Since we don't save anything to CDX server, this does not apply.
        """
        pass
