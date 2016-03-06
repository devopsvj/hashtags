# -*- coding: utf-8 -*-
"""
  Wikipedia Hashtags
  ~~~~~~~~~~~~~~~~~~

  Some scripts for hashtags in Wikipedia edit comments.

"""

import os
import oursql
from argparse import ArgumentParser
from time import strftime

from utils import find_hashtags, find_mentions
from dal import (db_connect, ht_db_connect, RecentChangesModel)

from log import tlog

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
        wiki_db_name = self.lang + 'wiki_p'
        wiki_db_host = self.lang + 'wiki.labsdb'
        self.wiki_connect = db_connect(wiki_db_name, wiki_db_host)
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
        rc_query = '''
            SELECT rc_id,
                   rc_timestamp,
                   rc_user,
                   rc_user_text,
                   rc_namespace,
                   rc_title,
                   rc_comment,
                   rc_minor,
                   rc_bot,
                   rc_new,
                   rc_cur_id,
                   rc_this_oldid,
                   rc_last_oldid,
                   rc_type,
                   rc_source,
                   rc_patrolled,
                   rc_ip,
                   rc_old_len,
                   rc_new_len,
                   rc_deleted,
                   rc_logid,
                   rc_log_type,
                   rc_log_action,
                   rc_params
            FROM recentchanges
            WHERE rc_type = 0
            AND rc_timestamp > DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? HOUR)
            AND rc_comment REGEXP ?
            ORDER BY rc_id DESC'''
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
        query = '''
            INSERT INTO recentchanges (htrc_id,
                                       htrc_lang,
                                       rc_id,
                                       rc_timestamp,
                                       rc_user,
                                       rc_user_text,
                                       rc_namespace,
                                       rc_title,
                                       rc_comment,
                                       rc_minor,
                                       rc_bot,
                                       rc_new,
                                       rc_cur_id,
                                       rc_this_oldid,
                                       rc_last_oldid,
                                       rc_type,
                                       rc_source,
                                       rc_patrolled,
                                       rc_ip,
                                       rc_old_len,
                                       rc_new_len,
                                       rc_deleted,
                                       rc_logid,
                                       rc_log_type,
                                       rc_log_action,
                                       rc_params)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
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
    prs.add_argument('--debug', default=DEBUG, action='store_true')
    return prs

@tlog.wrap('critical')
def main():
    tlog.critical('start').success('started {0}', os.getpid())
    parser = get_argparser()
    args = parser.parse_args()
    if args.debug:
        import log
        log.set_debug(True)
    rc = RecentChangeUpdater(lang=args.lang, debug=args.debug)
    rc.connect()
    rc.update_recentchanges(hours=args.hours)
    

if __name__ == '__main__':
    main()
