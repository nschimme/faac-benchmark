#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <faad3.h>
int main(int c,char**v){ FILE*f=fopen(v[1],"rb"),*o=fopen(v[2],"wb"); uint32_t l; fread(&l,4,1,f); uint8_t asc[64]; fread(asc,1,l,f);
 faad_config cfg; faad_config_init(&cfg,sizeof cfg); cfg.stream_format=FAAD_STREAM_RAW; cfg.output_format=FAAD_OUTPUT_16BIT;
 faad_decoder*d; faad_status s=faad_decoder_open(&cfg,asc,l,&d); if(s){printf("open: %s\n",faad_strerror(s));return 2;}
 faad_stream_info si={.struct_size=sizeof si}; faad_decoder_get_info(d,&si); uint8_t*pcm=malloc(si.max_output_bytes); int n=0,bad=0;
 uint8_t buf[8192]; while(fread(&l,4,1,f)==1){ fread(buf,1,l,f); uint32_t cons,w,fl; s=faad_decode_frame(d,buf,l,&cons,pcm,si.max_output_bytes,&w,&fl);
  if(s){bad++; if(bad<4) printf("frame %d: %s\n",n,faad_strerror(s));} else fwrite(pcm,1,w,o); n++; }
 faad_decoder_get_info(d,&si); printf("frames %d bad %d rate %u ch %u fsamples %u obj %d\n",n,bad,si.sample_rate,si.channels,si.frame_samples,(int)si.object_type); return 0;}
