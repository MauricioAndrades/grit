# Copyright (c) 2011-2012 Nathan Boley

MAX_NUM_TRANSCRIPTS = 50000
MIN_INTRON_CNT_FRAC = 0.001
VERBOSE = False
MIN_VERBOSE = False

import sys
import os
import numpy

from collections import namedtuple
CategorizedExons = namedtuple( "CategorizedExons", ["TSS", "TES", "internal"] )

import igraph
from cluster_exons import find_overlapping_exons, find_jn_connected_exons


sys.path.insert( 0, os.path.join( os.path.dirname( __file__ ), \
                                      "./exons/" ) )
from build_genelets import cluster_exons, cluster_overlapping_exons

sys.path.insert( 0, os.path.join( os.path.dirname( __file__ ), \
                                      "../sparsify/" ) )
import transcripts as transcripts_module

sys.path.append( os.path.join(os.path.dirname(__file__), "../file_types") )
from exons_file import parse_exons_files
from junctions_file import parse_jn_gffs, Junctions

from itertools import product, izip, chain

from collections import defaultdict
import multiprocessing
import Queue

class multi_safe_file( file ):
    def __init__( self, *args ):
        args = list( args )
        args.insert( 0, self )
        file.__init__( *args )
        self.lock = multiprocessing.Lock()

    def write( self, line ):
        self.lock.acquire()
        file.write( self, line + "\n" )
        self.flush()
        self.lock.release()

def build_gtf_line( gene_name, chr_name, gene_strand, trans_name, exon_num, \
                        start, stop ):
    gtf_line = list()
    
    if not chr_name.startswith( 'chr' ):
        chr_name = 'chr' + chr_name
    gtf_line.append( chr_name )
    gtf_line.append( 'build_transcripts' )
    gtf_line.append( 'exon' )
    gtf_line.append( str(start))
    gtf_line.append( str(stop))
    gtf_line.append( '.' )
    gtf_line.append( gene_strand )
    gtf_line.append( '.' )
    gtf_line.append( 'gene_id "' + gene_name + \
                         '"; transcript_id "' + trans_name + \
                         '"; exon_number "' + str(exon_num) + '";' )
    return '\t'.join( gtf_line )

def build_gtf_lines( gene_name, chrm, gene_strand, transcript_name, 
                     transcript, exons ):
    lines = []
    for exon_num, ( start, stop ) in \
            enumerate( exons[exon_index] for exon_index in transcript ):
        lines.append( build_gtf_line(gene_name, chrm, gene_strand, \
                                         transcript_name, exon_num, start, stop) )
        
    return lines

def cluster_exons( tss_exons, internal_exons, tes_exons, jns ):
    assert isinstance( tss_exons, set )
    assert isinstance( internal_exons, set )
    assert isinstance( tes_exons, set )
    
    all_exons = sorted( chain(tss_exons, internal_exons, tes_exons) )
    
    genes_graph = igraph.Graph()
    genes_graph.add_vertices( xrange(len(all_exons)) )
    genes_graph.add_edges( find_overlapping_exons(all_exons) )
    genes_graph.add_edges( find_jn_connected_exons(all_exons, jns ) )
    
    genes = genes_graph.components()
    for gene in genes:
        exons = [ all_exons[exon_i] for exon_i in gene ]
        yield CategorizedExons( tss_exons.intersection( exons ),
                                tes_exons.intersection( exons ),
                                internal_exons.intersection( exons ) )
    
    return

def build_transcripts( exons, jns ):
    # build a directed graph, with edges leading from exon to exon via junctions
    all_exons = sorted(chain(exons.TSS, exons.internal, exons.TES))
    graph = igraph.Graph(directed=True)
    graph.add_vertices( "TSS%i" % i for i in xrange(len(exons.TSS)) )
    graph.add_vertices( "I%i" % i for i in xrange(len(exons.internal)) )
    graph.add_vertices( "TES%i" % i for i in xrange(len(exons.TES)) )
    graph.add_edges( find_jn_connected_exons(all_exons, jns ) )
    for start

def build_genes( all_internal_exons, all_tss_exons, all_tes_exons, jns, jn_scores ):    
    keys = set(chain(all_tss_exons.iterkeys(), 
                     all_internal_exons.iterkeys(), 
                     all_tes_exons.iterkeys()))
    
    for (chrm, strand) in keys:
        if strand == '-': continue
        for gene_exons in cluster_exons( 
                set( map(tuple, all_tss_exons[(chrm, strand)].tolist()) ),
                set( map(tuple, all_internal_exons[(chrm, strand)].tolist()) ),
                set( map(tuple, all_tes_exons[(chrm, strand)].tolist() ) ),
                jns[(chrm, strand)]):
            print gene_exons
        
        #a = graph.get_adjlist()
        #n = source.index
        #m = dest.index


    sys.exit()
    genes_graph.add_nodes( xrange( len( gene_bndry_bins ) ) )


    def has_exon( exons, exon ):
        """Determine if exon is in sorted numpy array exons
        """
        index = exons[:,0].searchsorted( exon[0] )
        # search through all of the exons that start with the
        # correct coordinate and determine if the other coordinate
        # corresponds also
        while index < len(exons) and exon[0] == exons[index][0]:
            if exon[1] == exons[index][1]:
                return True
            index += 1
        return False
    
    
    cluster_num = 1
    clustered_exons = {}
    
    # create genelets for each chrm, strand combination that exists 
    # in both all exon types and junctions
    keys = set( exons ).intersection( jns ).intersection( \
        tss_exons ).intersection( tes_exons )
    
    for key in sorted(keys):
        chrm, strand = key
        all_chrm_exons = numpy.vstack( \
            (exons[key], tss_exons[key], tes_exons[key]) )
        
        genelets = cluster_exons( all_chrm_exons, jns[key] )
        
        # find tss and tes exons and add the grouped exons to the 
        # clustered_exons list
        for all_cluster_exons in genelets:
            cluster_tss_exons = []
            cluster_tes_exons = []
            for exon in all_cluster_exons:
                if has_exon( tss_exons[key], exon ):
                    cluster_tss_exons.append(exon)
                if has_exon( tes_exons[key], exon ):
                    cluster_tes_exons.append(exon)
            
            if len( cluster_tss_exons ) == 0 or len( cluster_tes_exons ) == 0:
                continue
            
            cluster_key = ("cluster_{0:d}".format( cluster_num ), chrm, strand)
            cluster_num += 1
            clustered_exons[ cluster_key ] = CategorizedExons( \
                cluster_tss_exons, cluster_tes_exons, all_cluster_exons)
    
    return clustered_exons

def find_transcripts( chrm, strand, junctions, start_exons, stop_exons, exons ):
    """Return transcripts from exons and junctions provided
    """
    def build_transcripts( exons, conn_exons, start_exons, stop_exons ):
        transcripts = []
        for transcript_index, entry in enumerate(
            transcripts_module.iter_transcripts( \
                exons, conn_exons, start_exons, stop_exons ) ):
            if transcript_index > MAX_NUM_TRANSCRIPTS:
                return 'TOO MANY TRANSCRIPTS'
            else:
                transcripts.append( entry )
        
        if len( transcripts ) == 0: return 'NO TRANSCRIPTS PRODUCED'
                
        return transcripts
    
    min_start, max_stop = min(min(i) for i in exons), max(max(i) for i in exons)
    conn_exons, conn_exon_scores = junctions.iter_connected_exons( \
        chrm, strand, min_start, max_stop, exons, True )

    if strand == '+':
        upstream_exons = start_exons
        downstream_exons = stop_exons
    else:
        assert strand == '-'
        upstream_exons = stop_exons
        downstream_exons = start_exons
    
    return build_transcripts(exons,conn_exons,upstream_exons,downstream_exons)

def build_transcripts_gtf_lines( gene_name, chrm, strand, exons, 
                                 junctions, junction_scores, log_fp ):
    """ Build the gtf lines corresponding with exons and junctions.
    """
    def write_log( gene_name, len_exons, len_trans, error_str="" ):
        return '{0},{1:d},{2:d},chr{3}:{4:d}-{5:d}:{6},"{7}"\n'.format( \
            gene_name, len_exons, len_trans, chrm, \
                min( min(i) for i in exons), \
                max( max(i) for i in exons), \
                strand, error_str )
    
    def get_indices( exons, some_exons ):
        indices = []
        for value in some_exons:
            index = exons.index( value )
            indices.append( index )
            
        return indices
    
    
    # unpack exons and convert start and stop exons to indices of 
    # sorted exons list
    tss_exons, tes_exons, all_exons = exons
    exons = sorted( all_exons )
    tss_exons = get_indices( exons, tss_exons )
    tes_exons = get_indices( exons, tes_exons )
    
    # filter introns by scores
    junctions_dict = dict( (tuple(jn), score) for jn, score
                           in izip( junctions[(chrm, strand)].tolist(), 
                                    junction_scores[(chrm, strand)] ) )
    intron_stops = [ start-1 for start, stop in exons ]
    intron_starts = [ stop+1 for start, stop in exons ]
    filtered_introns = [ (start, stop) for start, stop in 
                         product( intron_starts, intron_stops )
                         if start <= stop and (start,stop) in junctions_dict ]
    
    filtered_introns = dict((jn, junctions_dict[jn]) for jn in filtered_introns)
    max_score = float( max( filtered_introns.values() ) ) \
        if len( filtered_introns ) > 0 else 0
    if max_score < 1: 
        log_line = write_log(gene_name, len(exons), 0, 
                             "There were no junction reads." )
        log_fp.write( log_line + "\n" )
        return []
    
    assert max_score > 0
    filtered_jns = Junctions()
    for jn in filtered_introns:
        cnt = junctions_dict[jn]
        if cnt/max_score < MIN_INTRON_CNT_FRAC: 
            continue
        
        filtered_jns.add( chrm, strand, jn[0], jn[1], score=cnt )
        
    filtered_jns.freeze()
    
    transcripts = find_transcripts( \
        chrm, strand, filtered_jns, tss_exons, tes_exons, exons )
    
    # if too many or no transcripts were produced
    if isinstance( transcripts, str ):
        # log the region and note that too many transcripts were produced
        log_line = write_log(gene_name, len(exons), 0, transcripts)
        log_fp.write( log_line + "\n" )
        return []
    
    # otherwise, write them to GTF
    all_lines = []
    for transcript_index, transcript in enumerate( transcripts ):
        transcript_name = "{0}_{1:d}".format( gene_name, transcript_index )
        lines = build_gtf_lines( gene_name, chrm, strand, \
                                 transcript_name, transcript, exons )
        all_lines.extend( lines )
    
    log_fp.write( write_log(gene_name, len(exons), len( all_lines ) ) )
    
    return all_lines

def enumerate_transcripts_worker( input_queue, output_queue, 
                                  jns, jn_scores, log_fp ):
    while not input_queue.empty():
        try:
            ( gene_name, chrm, strand ), exons = input_queue.get(block=False)
        except Queue.Empty:
            break
        
        lines = build_transcripts_gtf_lines( \
            gene_name, chrm, strand, exons, jns, jn_scores, log_fp)
        
        output_queue.put( lines )
    
    return

def write_transcripts( genes, jns, jn_scores, log_fp, out_fp, threads ):
    # create queues to store input and output data
    manager = multiprocessing.Manager()
    input_queue = manager.Queue()
    output_queue = manager.Queue()
    
    for data, exons in genes.iteritems():
        input_queue.put( (data, exons) )
    
    args = ( input_queue, output_queue, jns, jn_scores, log_fp )
    # spawn threads to estimate genes expression
    processes = []
    for thread_id in xrange( threads ):
        p = multiprocessing.Process( 
            target=enumerate_transcripts_worker, args=args )
        p.start()
        processes.append( p )
    
    def process_queue():
        while not output_queue.empty():
            try:
                lines = output_queue.get()
            except Queue.Empty:
                break
            
            if len( lines ) > 0:
                out_fp.write( "\n".join( lines ) + "\n" )
    
    # process output queue
    while any( p.is_alive() for p in processes ):
        process_queue()
    
    # process any remaining lines
    process_queue()
    
    return

def parse_arguments():
    import argparse

    parser = argparse.ArgumentParser(description='Determine valid transcripts.')
    parser.add_argument( '--exons', type=file, required=True, nargs="+", \
        help='GFF file(s) contaning exons.')
    parser.add_argument( '--tss', type=file, required=True, nargs="+", \
        help='Gff file(s) containing valid transcription stop site exons. ' )
    parser.add_argument( '--tes', type=file, required=True, nargs="+", \
        help='Gff file(s) containing valid transcription stop site exons.')
    parser.add_argument( '--junctions', type=file, required=True, nargs="+", \
        help='Gff file(s) containing valid introns.')
    parser.add_argument( '--single-exon-genes', type=file, nargs="*", 
        help='Single exon genes file.')    

    parser.add_argument( '--threads', '-t', type=int , default=1, \
                             help='Number of threads spawn for multithreading.')
    parser.add_argument( '--out-fname', '-o', default='',\
                             help='Output file name. (default: stdout)')
    parser.add_argument( '--log-fname', '-l',\
                             help='Output log file name. (default: sterr)')
    parser.add_argument( '--verbose', '-v', default=False, action='store_true',\
                             help='Whether or not to print status information.')
    args = parser.parse_args()
    
    global VERBOSE
    VERBOSE = args.verbose
    transcripts_module.VERBOSE = VERBOSE
    
    out_fp = open( args.out_fname, "w" ) if args.out_fname else sys.stdout
    
    log_fp = multi_safe_file( args.log_fname, 'w' ) \
        if args.log_fname else sys.stderr
    
    return args.exons, args.tss, args.tes, args.junctions, \
        args.single_exon_genes, args.threads, out_fp, log_fp

def main():
    exon_fps, tss_exon_fps, tes_exon_fps, junction_fps, \
        single_exon_fps, threads, out_fp, log_fp = parse_arguments()
    
    if VERBOSE:
        print >> sys.stderr, 'Parsing input...'
    
    jns = parse_jn_gffs( junction_fps )    
    new_jns = defaultdict(list)
    new_jn_scores = defaultdict(list)
    for jn in jns:
        new_jns[(jn.chrm, jn.strand)].append( (jn.start, jn.stop) )
        new_jn_scores[(jn.chrm, jn.strand)].append( jn.cnt )
    
    jns = dict((key, numpy.array(val)) for key, val in new_jns.iteritems() )
    jn_scores = new_jn_scores
    
    internal_exons = parse_exons_files( exon_fps )
    tss_exons = parse_exons_files( tss_exon_fps )
    tes_exons = parse_exons_files( tes_exon_fps )
    
    if None != single_exon_fps:
        se_genes = parse_exons_files( single_exon_fps )
        gene_id = 1
        for (chrm, strand), exons in se_genes.iteritems():
            for start, stop in exons:
                gene_name = "single_exon_gene_%i" % gene_id
                out_fp.write( build_gtf_line( 
                    gene_name, chrm, strand, gene_name, 1, start, stop ) + "\n")
                gene_id += 1
    
    if VERBOSE:
        print >> sys.stderr, 'Clustering exons...'
    genes = build_genes( internal_exons, tss_exons, tes_exons, jns, jn_scores )

    
    
    if VERBOSE:
        print >> sys.stderr, 'Outputting all gene models...'
    write_transcripts( genes, jns, jn_scores, log_fp, out_fp, threads )
        
    return

if __name__ == "__main__":
    main()
