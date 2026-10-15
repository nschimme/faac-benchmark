#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <stdint.h>
static int16_t* rd(const char*p,long*n){FILE*f=fopen(p,"rb");fseek(f,0,SEEK_END);*n=ftell(f)/2;fseek(f,0,SEEK_SET);int16_t*b=malloc(*n*2);fread(b,2,*n,f);fclose(f);return b;}
int main(int c,char**v){long na,nb;int16_t*a=rd(v[1],&na),*b=rd(v[2],&nb);long lo=atol(v[3]),len=atol(v[4]),span=atol(v[5]);
 int ch=2; double best=1e300,bs=0;long bl=0;
 for(long lag=-span;lag<=span;lag++){double e=0,s=0;for(long i=lo;i<lo+len;i++){long j=i-lag; if(j<0||j>=nb) {e=1e300;break;} double d=(double)a[i]-b[j]; e+=d*d; s+=(double)a[i]*a[i];} if(e<best){best=e;bl=lag;bs=s;}}
 printf("lag %ld SNR %.1f dB (lens %ld %ld)\n",bl,10*log10(bs/(best+1)),na,nb);return 0;}
