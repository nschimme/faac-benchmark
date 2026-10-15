import struct,sys
src,dst=sys.argv[1],sys.argv[2]
d=open(src,'rb').read(); l=struct.unpack('<I',d[:4])[0]; asc=d[4:4+l]; pos=4+l
ascbits=''.join(format(b,'08b') for b in asc)
cfg='0'+'0'+'1'+'000000'+'0000'+'000'+ascbits+'000'+'11111111'+'0'+'0'   # useSameStreamMux=0, version0, sameTiming, subframes0, prog0, layer0, ASC, ftype0, fullness, other0, crc0
out=bytearray()
while pos<len(d):
    n=struct.unpack('<I',d[pos:pos+4])[0]; pos+=4; fr=d[pos:pos+n]; pos+=n
    bits=cfg
    pl=n; lenb=''
    while pl>=255: lenb+='11111111'; pl-=255
    lenb+=format(pl,'08b')
    bits+=lenb+''.join(format(b,'08b') for b in fr)
    bits+='0'*((-len(bits))%8)
    body=bytes(int(bits[i:i+8],2) for i in range(0,len(bits),8))
    hdr=(0x2B7<<13)|len(body)
    out+=bytes([hdr>>16&0xFF,hdr>>8&0xFF,hdr&0xFF])+body
open(dst,'wb').write(out)
