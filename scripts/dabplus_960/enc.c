/* usage: enc in.wav out.dab AOT(2|5|29) bitrate granule */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fdk-aac/aacenc_lib.h>
int main(int argc,char**argv){
  FILE*f=fopen(argv[1],"rb"); fseek(f,0,SEEK_END); long n=ftell(f); fseek(f,44,SEEK_SET);
  int aot=atoi(argv[3]), br=atoi(argv[4]), gl=atoi(argv[5]), ch=argc>6?atoi(argv[6]):2, sr=argc>7?atoi(argv[7]):48000;
  long ns=(n-44)/2; if(ns>sr*ch*10) ns=sr*ch*10;
  int16_t*pcm=malloc(ns*2); fread(pcm,2,ns,f);
  HANDLE_AACENCODER h; if(aacEncOpen(&h,0,ch)){puts("open");return 1;}
  #define P(a,v) if(aacEncoder_SetParam(h,a,v)){printf("param %s=%d fail\n",#a,(int)(v));return 1;}
  P(AACENC_AOT,aot); P(AACENC_SAMPLERATE,sr); P(AACENC_CHANNELMODE,ch==1?MODE_1:MODE_2);
  P(AACENC_BITRATE,br); P(AACENC_TRANSMUX,TT_MP4_RAW); P(AACENC_GRANULE_LENGTH,gl);
  if(aacEncEncode(h,NULL,NULL,NULL,NULL)!=AACENC_OK){puts("init fail");return 1;}
  AACENC_InfoStruct info; aacEncInfo(h,&info);
  FILE*o=fopen(argv[2],"wb"); uint32_t l=info.confSize; fwrite(&l,4,1,o); fwrite(info.confBuf,1,l,o);
  printf("ASC %u bytes, frameLength %u\n",l,info.frameLength);
  int in_id=IN_AUDIO_DATA,out_id=OUT_BITSTREAM_DATA,is=2,os=1;
  uint8_t out[8192]; long pos=0; int frames=0;
  for(;;){
    int n_in=(int)(ns-pos); if(n_in>(int)info.frameLength*ch) n_in=info.frameLength*ch; if(n_in<0) n_in=0;
    AACENC_BufDesc ib_={0},ob_={0}; AACENC_InArgs ia={0}; AACENC_OutArgs oa={0};
    void*ip=pcm+pos; int isz=n_in*2; void*op=out; int osz=sizeof out;
    ib_.numBufs=1; ib_.bufs=&ip; ib_.bufferIdentifiers=&in_id; ib_.bufSizes=&isz; ib_.bufElSizes=&is;
    ob_.numBufs=1; ob_.bufs=&op; ob_.bufferIdentifiers=&out_id; ob_.bufSizes=&osz; ob_.bufElSizes=&os;
    ia.numInSamples=(n_in>0)?n_in:-1;
    AACENC_ERROR e=aacEncEncode(h,&ib_,&ob_,&ia,&oa);
    if(e==AACENC_ENCODE_EOF) break; if(e!=AACENC_OK){printf("enc err %d\n",e);return 1;}
    if(n_in>0) pos+=oa.numInSamples;
    if(oa.numOutBytes>0){uint32_t b=oa.numOutBytes; fwrite(&b,4,1,o); fwrite(out,1,b,o); frames++;}
  }
  fclose(o); printf("%d frames\n",frames); return 0;}
