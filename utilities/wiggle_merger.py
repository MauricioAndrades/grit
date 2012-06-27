#!/usr/bin/python

# Copyright (c) 2011-2012 Nathan Boley

# import python mods
import os 
import sys

sys.path.append( os.path.join(os.path.dirname(__file__), "..", "file_types") )
from wiggle import Wiggle, GenomicInterval

VERBOSE = False

def parse_arguments():
    import argparse

    parser = argparse.ArgumentParser(description='Merge counts of many wiggle files into one wig file.')
    parser.add_argument( 'wigs', type=file, nargs='+', \
                             help='Wiggle files to merge into one wiggle.')
    parser.add_argument( '--chrm-sizes', type=file, required=True, \
                             help='A chromosome sizes file.')
    parser.add_argument( '--out-fname-prefix', default="merged_wiggle", \
                             help='Output filenames will be PREFIX.plus.wig and ' \
                             + "PREFIX.minus.wig. Default: %(default)s")
    parser.add_argument( '--track-name-prefix', default="merged_wig", \
                             help='The output track names will be PREFIX_plus and PREFIX_minus.')
    parser.add_argument( '--verbose', '-v', default=False, action='store_true', \
                             help='Whether or not to print status information.')
    parser.add_argument( '--filter-region', '-f', \
                             help='A filter region of the form chr:start-stop.')
    args = parser.parse_args()
    
    # set flag args
    global VERBOSE
    VERBOSE = args.verbose
    
    if args.filter_region != None:
        chrm, locs = args.filter_region.strip().split(":")
        start, stop = locs.split("-")
        chrm, start, stop = chrm.strip(), int(start), int(stop)
        filter_region = GenomicInterval( chrm, '.', start, stop )
    else:
        filter_region = None
    
    return args.wigs, args.out_fname_prefix, args.chrm_sizes, \
        args.track_name_prefix, filter_region

def main():
    wiggles, out_fname_prefix, chrm_sizes_fp, track_name_prefix, filter_region \
        = parse_arguments()
    
    merged_wiggle = Wiggle( chrm_sizes_fp )

    for input_wiggle_fp in wiggles:
        if VERBOSE:
            print "Adding ", input_wiggle_fp.fname
        merged_wiggle.load_data_from_fp( input_wiggle_fp )

    if VERBOSE:
        print 'Writing merged wiggle...'
    
    ofn_template = out_fname_prefix + ".{strand}.bedGraph"
    merged_wiggle.write_wiggles( ofn_template.format(strand='plus'),  \
                                 ofn_template.format(strand='minus'), \
                                 ignore_zeros=True, \
                                 track_name_prefix=track_name_prefix, \
                                 filter_region = filter_region )
    
if __name__ == '__main__':
    main()
