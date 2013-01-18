"""Preprocess download logs dumped from mongodb"""

import os
import struct
import subprocess
import tempfile
import itertools
import random
import heapq
import operator

import bson
import ujson

import util

data_dir = "/mnt/var/springer-recommendations/"

max_downloads_per_user = 1000

unpack_prefix = struct.Struct('i').unpack

# TODO: might be worth manually decoding the bson here and just picking out si/doi. could also avoid the utf8 encode later.
def from_dump(dump_filename):
    """Read a mongodb dump containing bson-encoded download logs"""
    with util.Timed('from_dump'):
        dump_file = open(dump_filename, 'rb')

        while True:
            prefix = dump_file.read(4)
            if len(prefix) == 0:
                break
            elif len(prefix) != 4:
                raise IOError('Prefix is too short: %s' % prefix)
            else:
                size, = unpack_prefix(prefix)
                data = prefix + dump_file.read(size - 4)
                download = bson.BSON(data).decode()
                if download.get('si', '') and download.get('doi', ''):
                    yield download

# have to keep an explicit reference to the stashes because many itertools constructs don't
stashes = []

class Stash():
    """On-disk cache of a list of rows"""
    def __init__(self):
        self.file = tempfile.NamedTemporaryFile(dir=data_dir)
        self.name = self.file.name
        stashes.append(self)

    def __iter__(self):
        with util.Timed('iter(%s)' % self.name):
            self.file.seek(0) # always iterate from the start
            return itertools.imap(ujson.loads, self.file)

    def __len__(self):
        with util.Timed('len(%s)' % self.name):
            result = subprocess.check_output(['wc', '-l', self.file.name])
            count, _ = result.split()
            return int(count)

    def save_as(self, name):
        os.rename(self.file.name, os.path.join(data_dir, name))

def stashed(rows):
    """Store a list of string rows in a temporary file"""
    if isinstance(rows, Stash):
        return rows
    else:
        stash = Stash()
        with util.Timed('stashed(%s)' % stash.name):
            stash.file.writelines(("%s\n" % ujson.dumps(row) for row in rows))
            stash.file.flush()
            return stash

def uniq_sorted(rows):
    """Return rows sorted by ujson order, with duplicate rows removed"""
    with util.Timed('uniq_sorted'):
        in_stash = stashed(rows)
        out_stash = Stash()
        subprocess.check_call(['sort', '-T', data_dir, '-u', in_stash.name, '-o', out_stash.name])
        return out_stash

def reverse_uniq_sorted(rows):
    """Return rows sorted by ujson order, with duplicate rows removed"""
    with util.Timed('reverse_uniq_sorted'):
        in_stash = stashed(rows)
        out_stash = Stash()
        subprocess.check_call(['sort', '-T', data_dir, '-u', '-r', in_stash.name, '-o', out_stash.name])
        return out_stash

def grouped(rows):
    """Return rows grouped by first column"""
    return itertools.groupby(rows, lambda r: r[0])

def edges(logs):
    with util.Timed('edges'):
        for log in logs:
            # There is honest-to-god unicode in here eg http://www.fileformat.info/info/unicode/char/2013/index.htm
            doi = log['doi'].encode('utf8')
            user = log['si'].encode('utf8')
            yield user, doi

def filter_bots(edges):
    with util.Timed('filter_bots'):
        for user, rows in grouped(uniq_sorted(edges)):
            dois = [row[1] for row in rows]
            if len(dois) < max_downloads_per_user: # TODO percentage of total downloads?
                for doi in dois:
                    yield doi, user
            else:
                util.log('filter_bots', 'filtering %s (%i downloads)' % (user, len(dois)))

def doi_rows(edges):
    with util.Timed('doi_rows'):
        for doi, rows in grouped(uniq_sorted(edges)):
            users = [row[1] for row in rows]
            yield doi, users

def min_hashes(doi_rows):
    """Minhash approximation as described by Das, Abhinandan S., et al. "Google news personalization: scalable online collaborative filtering." Proceedings of the 16th international conference on World Wide Web. ACM, 2007. """
    with util.Timed('min_hashes'):
        seed = random.getrandbits(64)
        for doi, users in doi_rows:
            hashes = [hash((seed, user)) for user in users]
            yield min(hashes), doi, users

def pairs(xs):
    for i, x1 in enumerate(xs):
        for x2 in xs[(i+1):]:
            yield x1, x2

def jaccard_similarity(users1, users2):
    return float(len(users1.intersection(users2))) / float(len(users1.union(users2)))

def scores(min_hashes):
    with util.Timed('scores'):
        for min_hash, group in grouped(uniq_sorted(min_hashes)):
            bucket = [(doi, set(users)) for (_, doi, users) in group]
            for (doi1, users1), (doi2, users2) in pairs(bucket):
                score = jaccard_similarity(users1, users2)
                yield doi1, score, doi2
                yield doi2, score, doi1

def recommendations(logs, iterations=1, top_n=5):
    with util.Timed('recommendations'):
        edges_stash = stashed(filter_bots(edges(logs)))
        doi_rows_stash = stashed(doi_rows(edges_stash))
        scores_iter = (scores(min_hashes(doi_rows_stash)) for _ in xrange(0, iterations))
        scores_stash = stashed(itertools.chain.from_iterable(scores_iter))
        for doi1, group in grouped(reverse_uniq_sorted(scores_stash)):
            top_scores = [(doi2, score) for (_, score, doi2) in itertools.islice(group, top_n)]
            yield doi1, top_scores

def main():
    logs = itertools.islice(from_dump('/mnt/var/Mongo3-backup/LogsRaw-20130113.bson'), 1000000)
    recs = recommendations(logs)
    stashed(recs).save_as('recs')

if __name__ == '__main__':
    # import cProfile
    # cProfile.run('main()', 'prof')
    main()
