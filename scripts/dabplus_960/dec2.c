#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <neaacdec.h>
int main(int c,char**v){ FILE*f=fopen(v[1],"rb"),*o=fopen(v[2],"wb"); uint32_t l; fread(&l,4,1,f); unsigned char asc[64]; fread(asc,1,l,f);
 NeAACDecHandle d=NeAACDecOpen(); NeAACDecConfigurationPtr cf=NeAACDecGetCurrentConfiguration(d); cf->outputFormat=FAAD_FMT_16BIT; cf->dontUpSampleImplicitSBR=0; NeAACDecSetConfiguration(d,cf);
 unsigned long sr; unsigned char ch; if(NeAACDecInit2(d,asc,l,&sr,&ch)<0){puts("init2 fail");return 2;}
 unsigned char buf[8192]; int n=0,bad=0; NeAACDecFrameInfo fi; while(fread(&l,4,1,f)==1){ fread(buf,1,l,f); void*p=NeAACDecDecode(d,&fi,buf,l); if(fi.error){bad++; if(bad<4)printf("frame %d: %s\n",n,NeAACDecGetErrorMessage(fi.error));} else fwrite(p,2,fi.samples,o); n++; }
 printf("frames %d bad %d rate %lu ch %u sbr %u\n",n,bad,fi.samplerate,fi.channels,fi.sbr); return 0;}
