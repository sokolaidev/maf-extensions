FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0b179206ab162b12a43bacea1b525a4023e0272005c92d08555c61b1beaedc40
RUN apk add --no-cache melange=0.63.2-r0 bubblewrap python-3.13
ADD --checksum=sha256:eed7e91702528f0072cf5424445888b9de2a9e32519c62873b98f83104d9201f https://codeload.github.com/wolfi-dev/os/tar.gz/65e46148155ff624c83ce3c02321614adc4d23b1 /tmp/wolfi.tar.gz
RUN mkdir /recipe && tar -xzf /tmp/wolfi.tar.gz -C /recipe --strip-components=1 && rm /tmp/wolfi.tar.gz
COPY configure.py build.sh /build/
RUN python3 /build/configure.py /recipe
WORKDIR /recipe
ENTRYPOINT ["sh", "/build/build.sh"]
