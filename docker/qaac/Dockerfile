FROM --platform=linux/amd64 alpine:latest
RUN apk add --update \
  7zip \
  curl \
  wine \
  && rm -rf /var/cache/apk/*

ARG QAAC_VERSION=2.80
ENV WINEDEBUG=-all
WORKDIR /qaac

RUN curl -LO https://github.com/nu774/qaac/releases/download/v${QAAC_VERSION}/qaac_${QAAC_VERSION}.zip && \
  7z e -y qaac_${QAAC_VERSION}.zip "qaac_${QAAC_VERSION}/x64/*.*" && \
  rm -v *.zip

RUN cd /tmp && \
  curl -LO 'https://secure-appldnld.apple.com/itunes12/091-56359-20171213-EDF2198A-E039-11E7-9A9F-D21A1E4B8CED/iTunes64Setup.exe' && \
  7z e -y iTunes64Setup.exe AppleApplicationSupport64.msi && \
  7z e -y AppleApplicationSupport64.msi '*'.dll && \
  for f in x64_AppleApplicationSupport_*; do mv "${f}" "${f#x64_AppleApplicationSupport_}"; done && \
  mv *.dll /qaac/ && \
  rm -rf /tmp/*

ENTRYPOINT ["wine64", "/qaac/qaac64.exe"]
