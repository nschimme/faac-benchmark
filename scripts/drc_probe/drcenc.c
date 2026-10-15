#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "aacenc_lib.h"
/* usage: drcenc in.wav out.aac bitrate profile(0..5) prog_ref_dB(or 'none') */
int main(int c,char**v){
  FILE*f=fopen(v[1],"rb"); uint8_t h[12]; fread(h,1,12,f); int ch=0,sr=0;
  for(;;){uint8_t ck[8]; if(fread(ck,1,8,f)!=8)return 1; uint32_t L=ck[4]|ck[5]<<8|ck[6]<<16|(uint32_t)ck[7]<<24;
    if(!memcmp(ck,"fmt ",4)){uint8_t b[16]; fread(b,1,16,f); ch=b[2]|b[3]<<8; sr=b[4]|b[5]<<8|b[6]<<16|b[7]<<24; fseek(f,L-16+(L&1),SEEK_CUR);}
    else if(!memcmp(ck,"data",4))break; else fseek(f,L+(L&1),SEEK_CUR);}
  HANDLE_AACENCODER e; aacEncOpen(&e,0,ch);
  aacEncoder_SetParam(e,AACENC_AOT,c>6?atoi(v[6]):2); aacEncoder_SetParam(e,AACENC_SAMPLERATE,sr);
  aacEncoder_SetParam(e,AACENC_CHANNELMODE,ch); aacEncoder_SetParam(e,AACENC_BITRATE,atoi(v[3]));
  aacEncoder_SetParam(e,AACENC_TRANSMUX,2); aacEncoder_SetParam(e,AACENC_AFTERBURNER,1);
  aacEncoder_SetParam(e,AACENC_METADATA_MODE,1);
  aacEncEncode(e,NULL,NULL,NULL,NULL);
  AACENC_MetaData md; memset(&md,0,sizeof md);
  md.drc_profile=atoi(v[4]); md.comp_profile=AACENC_METADATA_DRC_NONE;
  md.drc_TargetRefLevel=(int)(-31.0*65536); md.comp_TargetRefLevel=(int)(-31.0*65536);
  if(strcmp(v[5],"none")){md.prog_ref_level_present=1; md.prog_ref_level=(int)(atof(v[5])*65536);}
  FILE*o=fopen(v[2],"wb"); int16_t in[2048*8]; uint8_t out[8192];
  int n;
  for(;;){
    n=fread(in,2,1024*ch,f); if(n<=0)break;
    void*ib[2]={in,&md};
    int ids[2]={IN_AUDIO_DATA,IN_METADATA_SETUP}; int sz[2]={n*2,sizeof md}; int es[2]={2,sizeof md};
    AACENC_BufDesc i={0},ob={0},mb={0}; i.numBufs=2;i.bufs=ib;i.bufferIdentifiers=ids;i.bufSizes=sz;i.bufElSizes=es;
    void*obp=out;int oid=OUT_BITSTREAM_DATA,osz=sizeof out,oes=1; ob.numBufs=1;ob.bufs=&obp;ob.bufferIdentifiers=&oid;ob.bufSizes=&osz;ob.bufElSizes=&oes;
    AACENC_InArgs ia={n,0}; AACENC_OutArgs oa;
    if(aacEncEncode(e,&i,&ob,&ia,&oa)!=AACENC_OK)break; fwrite(out,1,oa.numOutBytes,o);
  }
  /* flush */
  for(;;){ AACENC_BufDesc i={0},ob={0}; void*obp=out;int oid=OUT_BITSTREAM_DATA,osz=sizeof out,oes=1; ob.numBufs=1;ob.bufs=&obp;ob.bufferIdentifiers=&oid;ob.bufSizes=&osz;ob.bufElSizes=&oes;
    AACENC_InArgs ia={-1,0}; AACENC_OutArgs oa; if(aacEncEncode(e,&i,&ob,&ia,&oa)!=AACENC_OK)break; if(!oa.numOutBytes)break; fwrite(out,1,oa.numOutBytes,o);}
  fclose(o); return 0;}
