# iron-proxy v0.50.0 with the policy patch in this build context.
FROM golang:1.26.1-alpine@sha256:2389ebfa5b7f43eeafbd6be0c3700cc46690ef842ad962f6c5bd6be49ed82039 AS build
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch v0.50.0 https://github.com/paradigmxyz/iron-proxy.git . \
    && test "$(git rev-parse HEAD)" = 5bd11abeb95ca734c767cfc992ea9be862700614
COPY iron.patch /tmp/iron.patch
RUN git apply --check /tmp/iron.patch && git apply /tmp/iron.patch
COPY credential_gate.go credential_gate_test.go /src/internal/proxy/
RUN go test ./internal/proxy -run '^TestMAFCredential' -count=1
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /iron-proxy ./cmd/iron-proxy

FROM alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0
RUN apk add --no-cache ca-certificates \
    && mkdir -p /run/maf-proxy /licenses \
    && chown 65532:65532 /run/maf-proxy
COPY --from=build /iron-proxy /usr/local/bin/iron-proxy
COPY --from=build /src/LICENSE /licenses/iron-proxy-LICENSE
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh
USER 65532:65532
EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
