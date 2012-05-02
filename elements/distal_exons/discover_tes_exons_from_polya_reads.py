#!/usr/bin/python

import sys
import os

VERBOSE = False

### Set default parameters ###
CVRG_FRACTION = 0.99

from collections import defaultdict

# import slide modules
import distal_exons
from distal_exons import find_all_distal_exons, convert_to_genomic_intervals

sys.path.append( os.path.join(
        os.path.dirname(__file__), "..", "..", 'file_types' ) )
import wiggle
from clustered_exons_file import parse_clustered_exons_file
from gtf_file import parse_gff_line, iter_gff_lines

def write_TESs( tes_s, out_fp, track_name="tes_exons" ):
    gff_iter = iter_gff_lines( \
        sorted(tes_s), source="disc_tes_from_polya", feature="TES_exon")
    
    out_fp.write( "track name=" + track_name + "\n" )    
    out_fp.write( "\n".join(gff_iter) + "\n" )
    
    return

def build_coverage_wig_from_polya_reads( tes_reads_fps, chrm_sizes_fp ):
    # build lists of the tes locations
    def add_reads_to_locs( tes_reads_fp, locs ):
        for line in tes_reads_fp:
            gff_l = parse_gff_line( line )
            # skip lines that weren't able to be parsed
            if gff_l == None: continue
            
            locs[ (gff_l.region.chr, gff_l.region.strand) ].append(
                gff_l.region.stop )
        
        return
    
    # process tes reads gff files (i.e. poly-A)
    locs = defaultdict( list )
    for tes_reads_fp in tes_reads_fps:
        add_reads_to_locs( tes_reads_fp, locs )
    
    # load locs data into a wiggle object
    tes_cvrg = wiggle.Wiggle( chrm_sizes_fp )
    tes_cvrg.load_data_from_positions( locs )
        
    return tes_cvrg

def parse_arguments():
    # global variables that can be set by arguments
    global CVRG_FRACTION
    
    import argparse
    parser = argparse.ArgumentParser(
        description='Get transcript end sites (TESs) from polya reads.')
    parser.add_argument(
        '--chrm-sizes', '-c', required=True, type=file,
        help='File with chromosome names and sizes.')
    parser.add_argument(
        '--clustered-exons-gtf', '-e', required=True, type=file,
        help="GTF file of exons associated with gene_id's.'")
    parser.add_argument(
        '--polya-read-gffs', '-r', required=True, type=file, nargs='+',
        help='GFF file which contains reads ending at a TES.')
    
    parser.add_argument(
        '--coverage-fraction', type=float, default=CVRG_FRACTION,
        help='Fraction of TES coverage to include in the TES refined exon '
        + 'boundaries. Default: %(default)f' )
    
    parser.add_argument(
        '--out-fname', '-o',
        help='Output file will be written to default. default: stdout')
    parser.add_argument(
        '--verbose', '-v', default=False, action='store_true',
        help='Whether or not to print status information.')
    args = parser.parse_args()
    
    out_fp = open( args.out_fname, "w" ) if args.out_fname else sys.stdout
    
    global VERBOSE
    VERBOSE = args.verbose
    distal_exons.VERBOSE = VERBOSE
    wiggle.VERBOSE = VERBOSE
    
    # Set parameter arguments
    CVRG_FRACTION = args.coverage_fraction
    
    return args.clustered_exons_gtf, args.chrm_sizes,\
        args.polya_read_gffs, out_fp

def main():
    # parse arguments
    clustered_exons_fp, chrm_sizes_fp, polya_read_gff_fps, out_fp, \
        = parse_arguments()
    
    if VERBOSE: print >> sys.stderr, "Loading clustered exons."
    clustered_exons = parse_clustered_exons_file( clustered_exons_fp )
    
    if VERBOSE: print >> sys.stderr, "Loading TES coverage wiggle."
    tes_cvg = build_coverage_wig_from_polya_reads(
        polya_read_gff_fps, chrm_sizes_fp )
            
    # actually find the tes exons, and convert them to genomic intervals
    if VERBOSE: print >> sys.stderr, 'Finding TES exons from filtered polya coverage.'
    tes_exons = find_all_distal_exons( clustered_exons, tes_cvg, False, CVRG_FRACTION)
    
    tes_exons = convert_to_genomic_intervals( tes_exons )
    
    if VERBOSE: print >> sys.stderr, 'Writing TES exons to a gff file.'    
    write_TESs( tes_exons, out_fp )
    out_fp.close()
    
    return

if __name__ == "__main__":
    main()
