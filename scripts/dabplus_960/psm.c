#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <stdint.h>
static int16_t* rd(const char*p,long*n){FILE*f=fopen(p,"rb");fseek(f,0,SEEK_END);*n=ftell(f)/2;fseek(f,0,SEEK_SET);int16_t*b=malloc(*n*2);fread(b,2,*n,f);fclose(f);return b;}
int main(int c,char**v){long na,nb;int16_t*a=rd(v[1],&na),*b=rd(v[2],&nb);long lo=atol(v[3]),len=atol(v[4]),span=atol(v[5]); /* in sample frames */
 double best=1e300,bs=0;long bl=0;
 for(long lag=-span;lag<=span;lag++){double e=0,s=0;for(long i=lo;i<lo+len;i++){long j=i-lag; double ma=(a[2*i]+a[2*i+1])*0.5, mb=(b[2*j]+b[2*j+1])*0.5; e+=(ma-mb)*(ma-mb); s+=ma*ma;} if(e<best){best=e;bl=lag;bs=s;}}
 /* stereo image: side/mid energy ratio, L/R balance */
 double ms=0,ss=0,ml=0,mr=0,ms2=0,ss2=0,ml2=0,mr2=0;
 for(long i=lo;i<lo+len;i++){double L=a[2*i],R=a[2*i+1]; ms+=((L+R)/2)*((L+R)/2); ss+=((L-R)/2)*((L-R)/2); ml+=L*L; mr+=R*R;
   long j=i-bl; double L2=b[2*j],R2=b[2*j+1]; ms2+=((L2+R2)/2)*((L2+R2)/2); ss2+=((L2-R2)/2)*((L2-R2)/2); ml2+=L2*L2; mr2+=R2*R2;}
 printf("mid SNR %.1f dB (lag %ld) | S/M dB: f3 %.1f ref %.1f | L/R dB: f3 %.1f ref %.1f\n",10*log10(bs/(best+1)),bl,10*log10(ss/ms),10*log10(ss2/ms2),10*log10(ml/mr),10*log10(ml2/mr2));return 0;}
