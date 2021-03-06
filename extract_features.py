#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright © 2011 University of Zürich
# Author: Rico Sennrich <sennrich@cl.uzh.ch>

import sys
import os
import re
import socket
import time
from subprocess import Popen, PIPE
from collections import defaultdict
from morphisto_getpos import get_true_pos
from config import SFST_BIN, MORPHISTO_MODEL, HOST, PORT

class MorphAnalyzer():
    """Base class for morphological analysis and feature extraction"""

    def __init__(self):
        
        self.posset = defaultdict(set)

        # Gertwol/Morphisto only partially analyze punctuation. This adds missing analyses.
        for item in ['(',')','{','}','"',"'",u'”',u'“','[',']',u'«',u'»','-',u'‒',u'–',u'‘',u'’','/','...','--']:
            self.posset[item].add('$(')
        self.posset[','].add('$,')
        for item in ['.',':',';','!','?']:
            self.posset[item].add('$.')

        #regex to check if word is alphanumeric
        #we don't use str.isalnum() because we want to treat hyphenated words as alphanumeric
        self.alphnum = re.compile(ur'^(?:\w|\d|-)+$', re.U)
        
        
    def create_features(self, line):
        """Create list of features for each word"""
        
        truth = ''
        pos = []
        linelist = line.split()
        
        if not linelist:
            return '\n'
        
        #feature: word itself
        word = linelist[0]
        
        #if input is already tagged, tag is added to end (for training / error analysis)
        if len(linelist) > 1:
            truth = linelist[1]
        
        #feature: is word uppercased?
        if word[0].isupper():
            feature_upper = 'uc'
        else:
            feature_upper = 'lc'

        #feature: is word alphanumeric?
        if self.alphnum.search(word[0]):
            feature_alnum = 'y'
        else:
            feature_alnum = 'n'

        #feature: list of possible part of speech tags
        if word in self.posset:
            pos = self.posset[word]
        for alternative in spelling_variations(word):
            if alternative in self.posset:
                pos = self.posset[alternative].union(pos)

        pos = sorted(pos)+['ZZZ']*10
        posstring = '\t'.join(pos[:10])

        outstring = (u"{w}\t{wlower}\t{upper}\t{alnum}\t{pos}".format(w=word, wlower=word.lower(), upper=feature_upper, pos=posstring, alnum=feature_alnum))

        if truth:
            outstring += '\t'+truth
            
        return outstring.encode("UTF-8")+'\n'



class GertwolAnalyzer(MorphAnalyzer):

    def analyze(self, inlines):
        """Call Gertwol analysis"""

        new = []

        #prepare gertwol analysis
        for line in inlines:
            
            linelist = line.split()
            if not linelist:
                continue
            
            word = linelist[0]
            if not word in self.posset:
                self.posset[word] = set([])
                new.append(word)

                #deal with spelling variations that Gertwol doesn't know
                for alternative in spelling_variations(word):
                    if not alternative in self.posset:
                        self.posset[alternative] = set([])
                        new.append(alternative)

        if new:
            morph_tool = Popen([os.path.join(sys.path[0], 'gertwol-wrapper.py')], stdin=PIPE, stdout=PIPE)
            analyses = morph_tool.communicate('\n'.join(new).encode("UTF-8"))[0]
            self.convert(analyses)

    
    def convert(self, analyses):
        """Convert Gertwol output into list of POS tags"""
        
        word = ''
        pos = ''
        for line in analyses.split('\n'):

            line = line.decode("UTF-8")

            if line.startswith('"<'):
                word = line[2:-2]
                continue

            linelist = line.split()
            i = 1
            pos = ''
            while len(linelist) > i:
                if linelist[i] in ['*']: #information we throw away
                    i += 1
                        
                if linelist[i] in ['TRENNBAR', 'PART', 'V', 'NUM', 'A', 'pre', 'post', 'ABK']:
                    if pos:
                        pos += ':'
                    pos += linelist[i]
                    i += 1
                    
                elif linelist[i] in ['S'] and len(linelist) > i+1 and linelist[i+1] in ['EIGEN']:
                    pos += ':'.join(linelist[i:i+2])
                    i += 2
                    break
                    
                else:
                    if pos:
                        pos += ':'
                    pos += linelist[i]
                    i += 1
                    break
                    
            if 'zu' in linelist: # distinguish between "aufhören" and "aufzuhören"
                pos += ':'+'zu'
                
            elif pos.startswith('A:') and len(linelist) > i+1: #distinguish between ADJA and ADJD
                pos += ':'+'flekt'
                
            if pos:
                self.posset[word].add(pos)


    def main(self):
        """do morphological analysis/feature extraction batchwise"""
        buf = []
        for i, line in enumerate(sys.stdin):
            line = line.decode('UTF-8')
            buf.append(line)
            
            if i and not i % 10000:
                self.analyze(buf)
                for line in buf:
                    sys.stdout.write(self.create_features(line))
                buf = []
                
        self.analyze(buf)
        for line in buf:
            sys.stdout.write(self.create_features(line))


class MorphistoAnalyzer(MorphAnalyzer):

    def __init__(self):
        MorphAnalyzer.__init__(self)
        
        #regex to get coarse POS tag from morphisto output
        self.re_mainclass = re.compile(u'<\+(.*?)>')
        
        self.p_server = self.server()

    
    def server(self):
        """Start a morphisto socket server. If one already exists, this will silently fail"""
        
        server = Popen([SFST_BIN, str(PORT), MORPHISTO_MODEL], stderr=open('/dev/null', 'w'))
        return server
    
    
    def client(self, words):
        """Communicate with morphisto socket server to obtain analysis of word list."""

        while True:
            
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((HOST, PORT))
                s.send(u'\n'.join(words).encode('UTF-8'))
                s.shutdown(socket.SHUT_WR)
                analyses = ''
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                    analyses += data
                    
                return analyses
               
            #If connection unsuccessful, there's two strategies:
            # - if server process is still running, we assume that the model is still being loaded, and simply wait
            # - if the server process has stopped, we assume that we so far used the morphisto server of another process, which now ended, and start a new one
            except socket.error:

                if self.p_server.poll():
                    sys.stderr.write('Morphisto server has stopped: starting new one\n')
                    self.p_server = self.server()
                time.sleep(0.1)

    
    def convert(self, analyses):
        """convert Morphisto output into list of POS tags"""
        
        word = ''
        for line in analyses.split('\n'):

            line = line.decode("UTF-8")

            if line.startswith('>'):
                word = line[2:]
                continue

            if line.startswith('no result'):
                continue
           
            try:
                raw_pos = self.re_mainclass.search(line).group(1)
            except:
                continue
            
            pos, pos2 = get_true_pos(raw_pos, line)
                            
            if pos:
                self.posset[word].add(pos)
            if pos2:
                self.posset[word].add(pos2)


    def process_line(self, line):
        """analyse the input morphologically and create features"""
        
        line = line.decode('UTF-8')
            
        linelist = line.split()
        if not linelist:
            return '\n'
        
        word = linelist[0]
        if not word in self.posset:
            todo = []
            self.posset[word] = set([])
            todo.append(word)

            #deal with spelling variations that Gertwol doesn't know
            for alternative in spelling_variations(word):
                if not alternative in self.posset:
                    self.posset[alternative] = set([])
                    todo.append(alternative)
                    
            analyses = self.client(todo)
            self.convert(analyses)
            
        return self.create_features(line)


    def main(self):
        """simple wrapper around process_line() which ensures that the server is terminated at the end.
           For a tighter integration of the analysis, process_line() can be directly called"""
        
        for line in sys.stdin:
            sys.stdout.write(self.process_line(line))
                
        self.p_server.terminate()


def spelling_variations(word):
    """Deal with spelling variations that morphology system may not know"""
            
    if word.startswith('Ae'):
        yield u"Ä" + word[2:]
    elif word.startswith(u'Oe'):
        yield u"Ö" + word[2:]
    elif word.startswith('Ue'):
        yield u"Ü" + word[2:]
        
    if "ss" in word:
        sharplist = word.split('ss')
        for i in range(len(sharplist)-1):
            yield sharplist[i]+u'ß'+sharplist[i+1]

    if u"ß" in word:
        sharplist = word.split(u'ß')
        for i in range(len(sharplist)-1):
            yield sharplist[i]+'ss'+sharplist[i+1]

    if "ae" in word:
        tmplist = word.split('ae')
        for i in range(len(tmplist)-1):
            yield tmplist[i]+u'ä'+tmplist[i+1]
        
    if "oe" in word:
        tmplist = word.split('oe')
        for i in range(len(tmplist)-1):
            yield tmplist[i]+u'ö'+tmplist[i+1]
        
    if "ue" in word:
        tmplist = word.split('ue')
        for i in range(len(tmplist)-1):
            yield tmplist[i]+u'ü'+tmplist[i+1]
   


if __name__ == '__main__':

    Analyzer = MorphistoAnalyzer()
    #Analyzer = GertwolAnalyzer()

    Analyzer.main()
