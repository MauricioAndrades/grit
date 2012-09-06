# Copyright (c) 2011-2012 Nathan Boley

import sys
import numpy

from itertools import product, izip
import re

from genomic_intervals import GenomicIntervals, GenomicInterval
from gtf_file import parse_gff_line, create_gff_line
from pysam import Fastafile
import itertools
from collections import defaultdict, namedtuple

VERBOSE = False

CONSENSUS_PLUS = 'GTAG'
CONSENSUS_MINUS = 'CTAC'

def get_jn_type( chrm, upstrm_intron_pos, dnstrm_intron_pos, 
                 fasta, jn_strand="UNKNOWN" ):
    # get first 2 bases from 5' and 3' ends of intron to determine 
    # splice junction strand by comparing to consensus seqs
    # subtract one from start since fasta is 0-based closed-open
    intron_seq = \
        fasta.fetch( 'chr'+chrm , upstrm_intron_pos-1, upstrm_intron_pos+1) + \
        fasta.fetch( 'chr'+chrm , dnstrm_intron_pos-2, dnstrm_intron_pos )
    
    # if junction matchs consensus set strand
    # else return non-canonical
    if intron_seq.upper() == CONSENSUS_PLUS:
        canonical_strand = '+'
    elif intron_seq.upper() == CONSENSUS_MINUS:
        canonical_strand = '-'
    else:
        if jn_strand == "UNKNOWN":
            return 'non-canonical', '.'
        else:
            return 'non-canonical'
    
    # if we don't know what the strand should be, then use the canonical seq
    # to infer the strand of the jn
    if jn_strand == "UNKNOWN":
        return 'canonical', canonical_strand
    # otherwise, if we know the jn's strand
    else:
        if jn_strand == canonical_strand:
            return 'canonical'
        return 'canonical_wrong_strand'
    
    assert False

_junction_named_tuple_slots = [
    "region", "type", "cnt", "uniq_cnt", "source_read_offset", "source_id" ]
    
_JnNamedTuple = namedtuple( "Junction", _junction_named_tuple_slots )
                 
class Junction( _JnNamedTuple ):
    valid_jn_types = set(( "infer", "canonical", 
                           "canonical_wrong_strand", "non_canonical" ))
    
    def __new__( self, region,
                 jn_type=None, cnt=None, uniq_cnt=None,
                 source_read_offset=None, source_id=None ):
        # do type checking
        #if not isinstance( region, GenomicInterval ):
        #    raise ValueError, "regions must be of type GenomicInterval"
        if region.strand not in ("+", "-" ):
            raise ValueError, "Unrecognized strand '%s'" % strand
        if jn_type != None and jn_type not in self.valid_jn_types:
            raise ValueError, "Unrecognized jn type '%s'" % jn_type
        
        if cnt != None: cnt = int( cnt )
        if uniq_cnt != None: uniq_cnt = int( uniq_cnt )
        
        return _JnNamedTuple.__new__( 
            Junction, region,
            jn_type, cnt, uniq_cnt, source_read_offset, source_id )
    
    chrm = property( lambda self: self.region.chr )
    strand = property( lambda self: self.region.strand )
    start = property( lambda self: self.region.start )
    stop = property( lambda self: self.region.stop )

    def build_gff_line( self, group_id=None, fasta_obj=None ):
        if self.type == None and fasta_obj != None:
            intron_type = get_jn_type( 
                self.chr, self.start, self.stop, fasta_obj, self.strand )
        else:
            intron_type = self.type
        
        group_id_str = str(group_id) if group_id != None else ""

        if self.source_read_offset != None:
            group_id_str += ' source_read_offset "{0}";'.format( 
                self.source_read_offset )
        
        if self.uniq_cnt != None:
            group_id_str += ' uniq_cnt "{0}";'.format( self.uniq_cnt )
        
        if intron_type != None:
            group_id_str += ' type "{0}";'.format( intron_type )
        
        count = self.cnt if self.cnt != None else 0
        
        return create_gff_line( 
            self.region, group_id_str, score=count, feature='intron' )

class Junctions( dict ):
    def __init__( self, jns ):
        for jn in jns:
            key = ( jn.chrm, jn.strand )
            if key not in self:
                self[key] = defaultdict( int )
            self[key][ ( jn.start, jn.stop ) ] += jn.cnt
        
        return
    
def parse_jn_line( line, return_tuple=False ):
    data = parse_gff_line( line )
    if data == None: return
    
    region = data[0]
    
    src_rd_offset = re.findall('source_read_offset "(\d+)";',data.group)
    src_rd_offset = None if len(src_rd_offset) == 0 else int(src_rd_offset[0])

    uniq_cnt = re.findall('uniq_cnt "(\d+)";',data.group)
    uniq_cnt = None if len(uniq_cnt) == 0 else int(uniq_cnt[0])
    
    return Junction( region, cnt=data.score,
                     source_read_offset=src_rd_offset, uniq_cnt=uniq_cnt )

def parse_jn_gff( input_file, send_tuples=False ):
    if isinstance( input_file, str ):
        fp = open( input_file )
    else:
        assert isinstance( input_file, file )
        fp = input_file
    
    jns = []    
    for line in fp:
        jn = parse_jn_line( line )
        if jn == None: 
            continue
        elif send_tuples:
            jns.append([ tuple(jn[0]), jn[1], jn[2], jn[3], jn[4], jn[5] ])
        else:
            jns.append( jn )
    
    if isinstance( input_file, str ):
        fp.close()
    
    return jns

def parse_jn_gffs( gff_fnames, num_threads=1 ):
    if num_threads == 1:
        jns = []
        for fname in gff_fnames:
            if VERBOSE:
                print >> sys.stderr, "Parsing '%s'" % fname
            jns.append( parse_jn_gff( fname ) )
        return jns                
    else:
        from multiprocessing import Process, Queue, Lock, Manager
        from Queue import Empty
        import marshal as cPickle
        
        manager = Manager()
        output_jns = manager.dict()
        
        gff_fname_queue = Queue()
        for i, fname in enumerate(gff_fnames):
            gff_fname_queue.put( ( i, fname ) )

        def foo( ipq ):
            while not ipq.empty():
                index, fname = ipq.get()
               
                if VERBOSE:
                    print >> sys.stderr, "Parsing '%s'" % fname
                
                jns = parse_jn_gff( fname, send_tuples=True )
                output_jns[index] = cPickle.dumps(jns) #, cPickle.HIGHEST_PROTOCOL)
                
                if VERBOSE:
                    print >> sys.stderr, "Finished parsing '%s'" % fname
            
            return

        ps = []
        for fname in gff_fnames:
            p = Process( target=foo,
                         args=[gff_fname_queue,] )
            p.start()
            ps.append( p )
        
        for p in ps:
            p.join()
        
        rv = []
        for index in xrange( len( gff_fnames ) ):
            if VERBOSE: 
                print >> sys.stderr, "Unpickling '%s'." % gff_fnames[index]
            rv.append( [] )
            for data in cPickle.loads( output_jns[index] ):
                data[0] = tuple.__new__( GenomicInterval,  data[0] )
                rv[-1].append( tuple.__new__( Junction, data ) )
        
        return rv
    

################################################################################
#
#
#  TODO: REMOVE EVERYTHING BELOW HERE
#
#

def build_jn_line( region, group_id, count=0, 
                   uniq_cnt=None, fasta_obj=None, intron_type=None):
    group_id_str = str( group_id ) #'group_id "{0}";'.format( str(group_id) )
    
    
    if intron_type == None and fasta_obj != None:
        intron_type = get_jn_type( region.chr, region.start, region.stop, \
                                       fasta_obj, region.strand )
    
    if uniq_cnt != None:
        group_id_str += ' uniq_cnt "{0}";'.format( uniq_cnt )        
    
    if intron_type != None:
        group_id_str += ' type "{0}";'.format( intron_type )
        
    jn_line = create_gff_line( region, group_id_str, score=count )
    return jn_line

    
def write_junctions( junctions, out_fp, scores=None, groups=None, 
                     fasta_fn=None, first_contig_len=None,
                     track_name="discovered_jns" ):
    """Output junctions with more than MIN_NONOVERLAPPING_READS in gff 
       format if apply_filter
    
    """
    if isinstance( junctions, Junctions ):
        assert scores == None and groups == None
        jns_iter = junctions.iter_jns_and_cnts_and_grps()
    else:
        if scores == None: scores = itertools.repeat( 0 )
        if groups == None: groups = itertools.repeat( '.' )
        jns_iter = izip( junctions, scores, groups )
        
    fasta_obj = None if fasta_fn == None else Fastafile( fasta_fn )
    
    out_fp.write( "track name={0}\n".format(track_name) )
    for region, count, grp in jns_iter:
        jn_line = build_jn_line( region, grp, count, fasta_obj )
        out_fp.write( jn_line + '\n' )
    
    return

def get_intron_bndry_sets( jns_fp ):
    """Get a dict of sets containing jns for each chrm, strand pair.
    
    The first returned value, upstream_jns, refers to the side of the
    intron that is *nearest* the promoter. 
    
    The eecond returned value, downstream_jns, refers to side
    nearest the transcript's 3' end. 
    """
    upstream_jns = defaultdict( set )
    downstream_jns = defaultdict( set )
    jns = defaultdict( set )
    for line in jns_fp:
        data = parse_gff_line( line )
        if data == None: continue
        pos = data[0]
        if pos.strand == '+':
            upstream_jns[ (pos.chr, pos.strand) ].add( pos.start  )
            downstream_jns[ (pos.chr, pos.strand) ].add( pos.stop  )
        else:
            assert pos.strand == '-'
            upstream_jns[ (pos.chr, pos.strand) ].add( pos.stop  )
            downstream_jns[ (pos.chr, pos.strand) ].add( pos.start  )

    return upstream_jns, downstream_jns
