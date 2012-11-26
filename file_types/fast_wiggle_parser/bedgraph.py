# Copyright (c) 2011-2012 Nathan Boley

import sys, os
import numpy
import signal
from ctypes import *
import numpy.ctypeslib
import multiprocessing as mp

VERBOSE = False

# load the gtf library
bedgraph_o = cdll.LoadLibrary( os.path.join( ".", \
        os.path.dirname( __file__), "libbedgraph.so" ) )

class c_contig_t(Structure):
    """
struct contig_t {
    char *name;
    int size;
    double* values;
};
"""
    _fields_ = [("name", c_char_p),
                ("size", c_int),
                ("values", POINTER(c_double))
               ]

class c_contigs_t(Structure):
    """
struct contigs_t {
    struct contig_t* contigs;
    int size;
};
"""
    _fields_ = [
                 ("contigs", POINTER(c_contig_t)),
                 ("size", c_int),
               ]

class Bedgraph( dict ):
    def __init__(self, c_contigs_p ):
        self.c_contigs_p = c_contigs_p
    
    def __del__(self):
        bedgraph_o.free_contigs( self.c_contigs_p )
        return

def load_bedgraph( fname ):
    c_contigs_p = c_void_p()
    bedgraph_o.load_bedgraph( c_char_p(fname), byref(c_contigs_p) )

    # check for a NULL pointer
    if not bool(c_contigs_p):
        raise IOError, "Couldn't load '%s'" % fname
    
    rv = Bedgraph( c_contigs_p )
    
    c_contigs = cast( c_contigs_p, POINTER(c_contigs_t) ).contents
    for i in xrange( c_contigs.size ):
        values = c_contigs.contigs[i].values
        size = c_contigs.contigs[i].size
        name = c_contigs.contigs[i].name
        
        if 0 == size:
            print >> sys.stderr, \
                "WARNING: found a 0 length chrm '%s' in '%s'. Skipping it." \
                % ( name, fname )
            continue
        
        array = numpy.ctypeslib.as_array( values, (size,))
        
        try:
            assert( not numpy.isnan( array.sum() ) )
        except:
            print fname, size, name
            print numpy.where( numpy.isnan(array) )
            raise

        rv[ name ] = array
    
    return rv

if __name__ == "__main__":
    raise NotImplementedError("This is a module")
    tracks = list( iter_bedgraph_tracks( sys.argv[1] ) )
    from multiprocessing import Pool
    def f(x):
        for name, track in tracks:
            print name, track, track.min(), track.sum()
        return
    
    p = Pool(20)
    p.map( f, range(200) )

# WAT?
