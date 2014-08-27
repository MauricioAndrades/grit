import os, sys
import cPickle

try: import grit
except ImportError: sys.path.insert(0, "/home/nboley/grit/grit/")

from grit.lib.multiprocessing_utils import ProcessSafeOPStream
from grit import config

from grit.files.reads import (
    CAGEReads, RAMPAGEReads, RNAseqReads, PolyAReads, fix_chrm_name_for_ucsc)
from grit.files.gtf import load_gtf

from grit import peaks

import multiprocessing
import Queue

def write_bedgraph_from_array(array, region, ofprefix):
    """
    track name=CAGE.pan..plus type=bedGraph
    chr4    89932   89933   4.00
    chr4    89955   89956   2.00
    chr4    89958   89959   2.00
   """
    chrm = region['chrm']
    if config.FIX_CHRM_NAMES_FOR_UCSC: 
        chrm = config.fix_chrm_name_for_ucsc(chrm)
    start = region['start']
    ofname = "%s.%s.bedgraph" % (
        ofprefix, {'+': 'plus', '-': 'minus'}[region['strand']])
    with open(ofname, 'w') as ofp:
        print >> ofp, "track name=%s type=bedGraph" % ofname
        for i, val in enumerate(array):
            if val < 1e-6: continue
            print >> ofp, "\t".join(
                (chrm, str(start+i), str(start+i+1), "%.2f" % val))
    return

def write_bedgraph(chrm, peaks, ofp):
    """
    track name=CAGE.pan..plus type=bedGraph
    chr4    89932   89933   4.00
    chr4    89955   89956   2.00
    chr4    89958   89959   2.00
   """
    if config.FIX_CHRM_NAMES_FOR_UCSC: chrm = fix_chrm_name_for_ucsc(chrm)
    for start, stop, value in peaks:
        ofp.write( "\t".join(
                (chrm, str(start), str(stop+1), "%.2f" % value)) + "\n")
    return

def process_genes(
        genes_queue, promoter_reads, rnaseq_reads, ofp_p, ofp_m):
    promoter_reads.reload()
    rnaseq_reads.reload()
    num_genes = genes_queue.qsize()
    while True:
        try: gene = genes_queue.get(timeout=1.0)
        except Queue.Empty: break
        
        if config.VERBOSE: config.log_statement(
                "Processing %s (%i\tremain)" % (
                    gene.id.ljust(30), genes_queue.qsize()))
        region_tuple = ( gene.chrm, gene.strand, 
                         max(0, gene.start-1000), gene.stop+1000)
        region = dict(zip(('chrm', 'strand', 'start', 'stop'), 
                          region_tuple))
        called_peaks = peaks.estimate_read_cov_and_call_peaks(
            region, promoter_reads, 'promoter', rnaseq_reads)
        ofp = ofp_p if region['strand'] == '+' else ofp_m
        write_bedgraph(region['chrm'], called_peaks, ofp)

    return

def parse_arguments():
    allowed_assays = ['cage', 'rampage', 'rnaseq', 'polya']
    
    import argparse
    parser = argparse.ArgumentParser(
        description='Call peaks from a RAMPAGE/CAGE experiment and matching RNASeq.')

    parser.add_argument( '--reference', type=file, required=True,
        help='GTF file containing genes to extract gene boundaries from.')
    
    parser.add_argument( '--rnaseq-reads', type=argparse.FileType('rb'), 
        help='BAM file containing mapped RNAseq reads.')
    parser.add_argument( '--rnaseq-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the first RNAseq read in a pair that maps to the genome without being reverse complemented is assumed to be on the correct strand. default: auto")
    parser.add_argument( '--num-mapped-rnaseq-reads', type=int,
        help="The total number of mapped rnaseq reads ( needed to calculate the FPKM ). This only needs to be set if it isn't found by a call to samtools idxstats." )
    
    parser.add_argument( '--cage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped cage reads.')
    parser.add_argument( '--cage-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the reads that maps to the genome without being reverse complemented are assumed to be on the '+'. default: auto")

    parser.add_argument( '--rampage-reads', type=argparse.FileType('rb'),
        help='BAM file containing mapped rampage reads.')
    parser.add_argument( '--rampage-read-type', 
                         choices=["forward", "backward", "auto"],
                         default='auto',
        help="If 'forward' then the first read in a pair that maps to the genome without being reverse complemented are assumed to be on the '+' strand. default: auto")
    
    parser.add_argument( '--out-fname-prefix', '-o', default="peaks",
                         help='Output files will be named (out-fname-prefix).STRAND.bed')
    
    parser.add_argument( '--ucsc', default=False, action='store_true', 
                         help='Format the contig names to work with the UCSC genome browser.')

    parser.add_argument( '--min-merge-distance', default=10, type=int,
                         help='The distance in basepairs under whihc peaks will be merged .')
    parser.add_argument( '--min-relative-merge-distance', default=0.5, type=float,
                         help='The distance as a fraction of a peak size under which peaks will be merged .')
    parser.add_argument( '--trim-fraction', default=0.01, type=float,
                         help='The fraction of reads that will be trimmed from merged reads.')
    parser.add_argument( '--exp-filter-fraction', default=0.01, type=float,
                         help='Peaks with a relative expression fraction under this amount will be filtered.')
        
    parser.add_argument( '--verbose', '-v', default=False, action='store_true', 
                         help='Whether or not to print status information.')
    parser.add_argument( '--threads', '-t', default=1, type=int,
                         help='The number of threads to run.')

        
    args = parser.parse_args()
    config.VERBOSE = args.verbose
    config.FIX_CHRM_NAMES_FOR_UCSC = args.ucsc
    config.NTHREADS = args.threads
    
    peaks.MIN_MERGE_SIZE = args.min_merge_distance
    peaks.MIN_REL_MERGE_SIZE = args.min_relative_merge_distance
    
    peaks.TRIM_FRACTION = args.trim_fraction
    peaks.MAX_EXP_FRACTION = args.exp_filter_fraction
    
    ref_genes = load_gtf(args.reference)
    
    if args.cage_reads != None:
        assert args.rampage_reads == None, "Can not use RAMPAGE and CAGE reads"
        if config.VERBOSE: config.log_statement( "Loading %s" % args.cage_reads.name )
        rev_reads = {'forward':False, 'backward':True, 'auto': None}[
            args.cage_read_type]
        promoter_reads = CAGEReads(args.cage_reads.name, "rb").init(
            reverse_read_strand=rev_reads, ref_genes=ref_genes)
    elif args.rampage_reads != None:
        assert args.cage_reads == None, "Can not use RAMPAGE and CAGE reads"
        if config.VERBOSE: 
            config.log_statement( "Loading %s" % args.rampage_reads.name )
        rev_reads = {'forward':False, 'backward':True, 'auto': None}[
            args.rampage_read_type]
        promoter_reads = RAMPAGEReads(args.rampage_reads.name, "rb").init(
            reverse_read_strand=rev_reads, ref_genes=ref_genes)
    else:
        assert False, "RAMPAGE or CAGE reads must be set"

    rev_reads = {'forward':False, 'backward':True, 'auto': None}[
        args.rnaseq_read_type]
    rnaseq_reads = RNAseqReads(args.rnaseq_reads.name, "rb").init(
        reverse_read_strand=rev_reads, ref_genes=ref_genes)
    
    return ref_genes, promoter_reads, rnaseq_reads, args.out_fname_prefix

def main():
    genes, promoter_reads, rnaseq_reads, ofprefix = parse_arguments()
    
    ofp_p = ProcessSafeOPStream(open("%s.plus.bedgraph" % ofprefix, 'w'))
    print >> ofp_p, "track name=%s.plus type=bedGraph" % ofprefix
    ofp_m = ProcessSafeOPStream(open("%s.minus.bedgraph" % ofprefix, 'w'))
    print >> ofp_m, "track name=%s.minus type=bedGraph" % ofprefix

    queue = multiprocessing.Queue()
    for gene in genes:
        queue.put(gene)
        
    args = [queue, promoter_reads, rnaseq_reads, ofp_p, ofp_m]
    ps = []
    for i in xrange(config.NTHREADS):
        p = multiprocessing.Process(target=process_genes, args=args)
        p.daemon=True
        p.start()
        ps.append(p)
        
    for p in ps: p.join()

    ofp_p.close()
    ofp_m.close()
    
    return

if __name__ == '__main__':
    main()
