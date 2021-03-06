# -*- coding: utf-8 -*-
"""
  Wikipedia Hashtags
  ~~~~~~~~~~~~~~~~~~

  Some scripts for hashtags in Wikipedia edit comments.

"""
import os
import sys
import json
import uuid
import time
import random
import traceback
from time import strftime
from pipes import quote as shell_quote
from argparse import ArgumentParser


import oursql

from utils import find_hashtags, find_mentions
from dal import wiki_db_connect, ht_db_connect, RecentChangesModel, RC_COLUMNS
from log import tlog

RUN_UUID = str(uuid.uuid4())

DEFAULT_HOURS = 24
DEFAULT_LANG = 'en'

DEBUG = False

# MySQL translations of https://gist.github.com/mahmoud/237eb20108b5805aed5f
MYSQL_HASHTAG_RE = '(^|[[:blank:]]|\\.|\\!|\\/)[#＃][[:alnum:]]+[[:>:]]'
MYSQL_MENTION_RE = '(^|[[:blank:]]|\\.|\\!|\\/)[@][^\s#<>[\]|{}]+[[:>:]]'


class RecentChangeUpdater(object):
    def __init__(self, lang=DEFAULT_LANG, debug=DEBUG):
        self.lang = lang
        self.debug = debug
        self.ht_id_map = {}
        self.htrc_id_map = {}
        self.mn_id_map = {}
        self.htrc_id_map = {}
        self.htrc_id_mn_map = {}
        self.stats = {'changes_added': 0, 'tags_added': 0, 'mentions_added': 0}

    @tlog.wrap('critical')
    def connect(self):
        self.wiki_connect = wiki_db_connect(self.lang, db_host='analytics')
        self.ht_connect = ht_db_connect()

    def _wiki_execute(self, query, params, as_dict=False):
        if as_dict:
            wiki_cursor = self.wiki_connect.cursor(oursql.DictCursor)
        else:
            wiki_cursor = self.wiki_connect.cursor()
        try:
            wiki_cursor.execute(query, params)
        except Exception as e:
            import pdb; pdb.set_trace()
        if self.debug and wiki_cursor.rowcount > 0:
            print 'affected %s rows' % wiki_cursor.rowcount
        return wiki_cursor

    def _ht_execute(self, query, params, as_dict=False, **kwargs):
        ignore_dups = kwargs.pop('ignore_dups', False)
        if kwargs:
            raise TypeError('got unexpected kwargs: %r' % kwargs.keys())
        if as_dict:
            ht_cursor = self.ht_connect.cursor(oursql.DictCursor)
        else:
            ht_cursor = self.ht_connect.cursor()
        try:
            ht_cursor.execute(query, params)
        except oursql.CollatedWarningsError as e:
            #import pdb;pdb.set_trace()
            pass
        except oursql.IntegrityError as ie:
            error_code = ie[0]
            if not (error_code == 1062 and ignore_dups):
                raise
        except Exception as e:
            #import pdb; pdb.set_trace()
            raise
        if self.debug and ht_cursor.rowcount > 0:
            #print 'affected %s rows' % ht_cursor.rowcount
            pass
        return ht_cursor

    @tlog.wrap('critical', inject_as='log_rec')
    def update_recentchanges(self, hours, log_rec):
        changes = self.find_recentchanges(hours)
        self.stats['total_changes'] = len(changes)
        for change in changes:
            change = RecentChangesModel(*change)
            hashtags = find_hashtags(change.rc_comment)
            hashtags = [ht.lower().encode('utf-8') for ht in hashtags]
            mentions = find_mentions(change.rc_comment)
            mentions = [m.lower().encode('utf-8') for m in mentions]
            htrc_id = self.add_recentchange(change)
            self.htrc_id_map[htrc_id] = hashtags
            self.htrc_id_mn_map[htrc_id] = mentions
            for hashtag in hashtags:
                self.add_hashtag(hashtag, change.rc_timestamp)
            for mention in mentions:
                self.add_mention(mention, change.rc_timestamp)
        self._update_ht_rc_mapping()
        self._update_mn_rc_mapping()
        timestamp = strftime('%Y-%m-%d %H:%M:%S')
        tags = self.stats['total_tags']
        new_tags = self.stats['tags_added']
        log_rec['lang'] = self.lang
        log_rec['hours'] = hours
        log_rec['new_changes'] = self.stats['total_changes']
        log_rec['new_tags'] = self.stats['tags_added']
        log_rec['new_mentions'] = self.stats['mentions_added']
        mentions = self.stats['total_mentions']
        new_mentions = self.stats['mentions_added']
        changes = self.stats['total_changes']
        new_changes = self.stats['changes_added']
        log_rec.success('Searched {lang} for {hours} hours, and found {new_changes}'
                        ' revs with {new_tags} tags and {new_mentions} mentions')
        return self.stats

    @tlog.wrap('debug')
    def _update_ht_rc_mapping(self):
        for htrc_id, hashtags in self.htrc_id_map.items():
            for hashtag in hashtags:
                ht_id = self.ht_id_map[hashtag]
                if not ht_id:
                    import pdb;pdb.set_trace()
                    raise Exception('Missing ht_id')
                query = '''
                    INSERT INTO hashtag_recentchanges
                    VALUES (?, ?)'''
                params = (ht_id, htrc_id)
                cursor = self._ht_execute(query, params, ignore_dups=True)
        # TODO
        self.stats['total_tags'] = len(set([h for hs in
                                            self.htrc_id_map.values()
                                            for h in hs]))

    @tlog.wrap('debug')
    def _update_mn_rc_mapping(self):
        for htrc_id, mentions in self.htrc_id_mn_map.items():
            for mention in mentions:
                mn_id = self.mn_id_map[mention]
                if not mn_id:
                    raise Exception('missing mn_id')
                query = '''
                    INSERT INTO mention_recentchanges
                    VALUES (?, ?)'''
                params = (mn_id, htrc_id)
                cursor = self._ht_execute(query, params, ignore_dups=True)
        # TODO
        self.stats['total_mentions'] = len(set([m for ms in
                                                self.htrc_id_mn_map.values()
                                                for m in ms]))
    @tlog.wrap('debug')
    def find_recentchanges(self, hours):
        rc_cols_str = ', '.join(RC_COLUMNS)
        rc_query_tmpl = '''
            SELECT %s
            FROM recentchanges
            WHERE rc_timestamp > DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? HOUR)
            AND rc_type != 5
            AND rc_comment REGEXP ?
            ORDER BY rc_id DESC'''
        rc_query = rc_query_tmpl % rc_cols_str
        rc_params = (hours, MYSQL_HASHTAG_RE)
        cursor = self._wiki_execute(rc_query, rc_params)
        changes = cursor.fetchall()
        return changes

    @tlog.wrap('debug')
    def add_recentchange(self, rc):
        query = '''
            SELECT htrc_id
            FROM recentchanges
            WHERE rc_id = ?
            AND htrc_lang = ?
            LIMIT 1'''
        params = (rc[0], self.lang)
        cursor = self._ht_execute(query, params)
        htrc_id = cursor.fetchall()
        if htrc_id:
            return htrc_id[0][0]
        rc_cols_str = ', '.join(RC_COLUMNS)
        query_tmpl = '''
            INSERT INTO recentchanges (htrc_id, htrc_lang, %s)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
        query = query_tmpl % rc_cols_str
        params = (None, self.lang) + rc
        cursor = self._ht_execute(query, params)
        self.stats['changes_added'] += 1
        return cursor.lastrowid

    @tlog.wrap('debug')
    def get_mn_id(self, mention):
        query = '''
            SELECT mn_id, mn_update_timestamp
            FROM mentions
            WHERE mn_text = ?
            LIMIT 1'''
        params = (mention,)
        cursor = self._ht_execute(query, params)
        mn_res = cursor.fetchall()
        return mn_res

    @tlog.wrap('debug')
    def get_ht_id(self, hashtag):
        query = '''
            SELECT ht_id, ht_update_timestamp
            FROM hashtags
            WHERE ht_text = ?
            LIMIT 1'''
        params = (hashtag,)
        cursor = self._ht_execute(query, params)
        ht_res = cursor.fetchall()
        return ht_res

    @tlog.wrap('debug')
    def add_mention(self, mention, rc_timestamp):
        # if mention in self.mn_id_map:
        #     return self.mn_id_map[mention]
        mn_res = self.get_mn_id(mention)
        if mn_res:
            self.mn_id_map[mention] = mn_res[0][0]
            if mn_res[0][1] < rc_timestamp:
                query = '''
                    UPDATE mentions
                    SET mn_update_timestamp = ?
                    WHERE mn_text = ?'''
                params = (rc_timestamp, mention)
                cursor = self._ht_execute(query, params)
                self.stats['mentions_added'] += 1
            return self.mn_id_map[mention]
        query = '''
            INSERT INTO mentions
            VALUES (?, ?, UTC_TIMESTAMP() + 0, ?)'''
        params = (None, mention, rc_timestamp)
        cursor = self._ht_execute(query, params)
        # TODO: returns None?
        self.mn_id_map[mention] = cursor.lastrowid
        if not self.mn_id_map.get(mention):
            ht_res = self.get_ht_id(mention)
            self.mn_id_map[mention] = ht_res[0][0]
        self.stats['tags_added'] += 1
        return self.mn_id_map[mention]

    @tlog.wrap('debug')
    def add_hashtag(self, hashtag, rc_timestamp):
        # if hashtag in self.ht_id_map:
        #     return self.ht_id_map[hashtag]
        ht_res = self.get_ht_id(hashtag)
        if ht_res:
            self.ht_id_map[hashtag] = ht_res[0][0]
            if ht_res[0][1] < rc_timestamp:
                query = '''
                    UPDATE hashtags
                    SET ht_update_timestamp = ?
                    WHERE ht_text = ?'''
                params = (rc_timestamp, hashtag)
                cursor = self._ht_execute(query, params)
                self.stats['tags_added'] += 1
            return self.ht_id_map[hashtag]
        query = '''
            INSERT INTO hashtags
            VALUES (?, ?, UTC_TIMESTAMP() + 0, ?)'''
        params = (None, hashtag, rc_timestamp)
        cursor = self._ht_execute(query, params)
        self.ht_id_map[hashtag] = cursor.lastrowid  # Why does this return None?
        if not self.ht_id_map.get(hashtag):
            ht_res = self.get_ht_id(hashtag)
            self.ht_id_map[hashtag] = ht_res[0][0]
        self.stats['tags_added'] += 1
        return self.ht_id_map[hashtag]


def get_argparser():
    desc = 'Update the database of hashtags'
    prs = ArgumentParser(description=desc)
    prs.add_argument('--lang', default=DEFAULT_LANG)
    prs.add_argument('--hours', default=DEFAULT_HOURS)
    prs.add_argument('--jitter', type=int, default=0)
    prs.add_argument('--debug', default=DEBUG, action='store_true')
    return prs


def get_command_str():
    return ' '.join([sys.executable] + [shell_quote(v) for v in sys.argv])


class RunLogDAL(object):
    def __init__(self):
        pass

    def add_start_record(self, lang, command=None, run_uuid=None):
        if not command:
            command = get_command_str()
        if len(command) > 1024:
            command = command[:1024]
        if not run_uuid:
            run_uuid = RUN_UUID
        params = (lang, command, run_uuid)
        conn = ht_db_connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute('INSERT INTO start_log'
                               ' (lang, command, run_uuid)'
                               ' VALUES (?, ?, ?)', params)
        finally:
            conn.close()
        return

    def add_complete_record(self, lang, output=None, run_uuid=None):
        output = output or ''
        if len(output) > 1024:
            output = output[:4096]
        if not run_uuid:
            run_uuid = RUN_UUID
        params = (lang, output, run_uuid)
        conn = ht_db_connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute('INSERT INTO complete_log'
                               ' (lang, output, run_uuid)'
                               ' VALUES (?, ?, ?)', params)
        finally:
            conn.close()
        return


@tlog.wrap('critical')
def main():
    with tlog.critical('start') as act:
        parser = get_argparser()
        args = parser.parse_args()
        wait = random.randint(0, args.jitter)
        act.success('started pid {process_id}, fetch for {lang} beginning in {wait} seconds', lang=args.lang, wait=wait)
        time.sleep(wait)
    
    run_logger = RunLogDAL()
    run_logger.add_start_record(lang=args.lang)
    output = '{}'
    try:
        if args.debug:
            import log
            log.set_debug(True)

        rcu = RecentChangeUpdater(lang=args.lang, debug=args.debug)
        rcu.connect()
        rcu.update_recentchanges(hours=args.hours)
        output = json.dumps(rcu.stats)
    except Exception:
        output = json.dumps({'error': traceback.format_exc()})
        raise
    finally:
        run_logger.add_complete_record(lang=args.lang, output=output)


if __name__ == '__main__':
    main()
