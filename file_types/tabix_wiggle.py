import os, sys
import pysam
import numpy

from chrm_sizes import ChrmSizes

def guess_strand_from_fname( fname ):
    if fname.lower().rfind( "plus" ) >= 0:
        return '+'
    elif fname.lower().rfind( "+" ) >= 0:
        return '+'
    elif fname.lower().rfind( "minus" ) >= 0:
        return '-'
    elif fname.lower().rfind( "-" ) >= 0:
        return '-'
    else:
        raise ValueError, "Couldn't infer strand from filename '%s'" % fname
    
    assert False

class TabixBackedArray(object):
    def __init__( self, files, chrm, strand=None, contig_len=None ):
        self._data_files = [ pysam.Tabixfile( file ) 
                             if type(file) == str else file 
                             for file in files ]
        self.chrm = chrm
        self.strand = strand
        self.contig_len = contig_len
        return
    
    def __getitem__( self, item ):
        rv = numpy.zeros( item.stop - item.start + 1 )
        for data_file in self._data_files:
            for line in data_file.fetch( 
                    self.chrm, item.start, item.stop, parser=pysam.asTuple() ):
                rv[int(line[1])-item.start:int(line[2])-item.stop] += float(line[3])
        
        return rv
    
    def as_array( self, contig_len=None ):
        if contig_len == None:
            contig_len = self.contig_len
        return self[0:contig_len]

class Wiggle( dict ):
    def __init__( self, chrm_sizes_fp, fps, strands=None ):
        self.chrm_sizes = ChrmSizes( chrm_sizes_fp.name )
        
        for i, fp in enumerate(fps):
            fname = fp.name
            # find the strand
            strand = strands[i] if strands != None \
                else guess_strand_from_fname(fname)
            
            # compress the file if necessary
            if not fname.endswith('.gz'):
                try:
                    pysam.tabix_compress( fname, fname + '.gz')
                # if the file already exists, assume it is fine
                except IOError:
                    pass
                fname = fname + '.gz'
            
            # index the file, if necessary
            try:
                pysam.tabix_index( fname, preset='bed' )
            except IOError:
                pass
            
            for contig, contig_size in self.chrm_sizes.iteritems():
                if not self.has_key((contig, strand)):
                    self[(contig, strand)] = []
                self[(contig, strand)].append( fname )
        
        for (contig, strand), vals in self.iteritems():
            self[(contig, strand)] = TabixBackedArray( 
                self[(contig, strand)], contig, strand, self.chrm_sizes[contig])
        
        return

if __name__ == '__main__':
    x = Wiggle( open(sys.argv[1]), [ open( x ) for x in sys.argv[2:] ] )
    print x
    print x.keys()
