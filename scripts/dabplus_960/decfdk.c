#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <fdk-aac/aacdecoder_lib.h>
int main(int c,char**v){ FILE*f=fopen(v[1],"rb"),*o=fopen(v[2],"wb"); uint32_t l; fread(&l,4,1,f); UCHAR asc[64]; fread(asc,1,l,f);
 HANDLE_AACDECODER d=aacDecoder_Open(TT_MP4_RAW,1); UCHAR*cf[1]={asc}; UINT cl[1]={l};
 if(aacDecoder_ConfigRaw(d,cf,cl)){puts("cfg fail");return 2;}
 aacDecoder_SetParam(d,AAC_PCM_MAX_OUTPUT_CHANNELS,2);
 static UCHAR buf[8192]; static INT_PCM pcm[8192*4]; int n=0,bad=0; uint32_t sz;
 while(fread(&l,4,1,f)==1){ fread(buf,1,l,f); UCHAR*b[1]={buf}; UINT bl[1]={l}, valid=l; sz=l;
   if(aacDecoder_Fill(d,b,bl,&valid)){bad++;continue;}
   AAC_DECODER_ERROR e=aacDecoder_DecodeFrame(d,pcm,sizeof pcm/sizeof(INT_PCM),0);
   if(e!=AAC_DEC_OK){bad++; if(bad<4)printf("frame %d err %x\n",n,e);} else { CStreamInfo*si=aacDecoder_GetStreamInfo(d); fwrite(pcm,2,si->frameSize*si->numChannels,o);} n++; }
 CStreamInfo*si=aacDecoder_GetStreamInfo(d); printf("frames %d bad %d rate %d ch %d frameSize %d aot %d\n",n,bad,si->sampleRate,si->numChannels,si->frameSize,si->aot); return 0;}
