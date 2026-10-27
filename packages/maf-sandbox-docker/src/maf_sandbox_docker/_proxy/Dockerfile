# iron-proxy v0.50.0 with the policy patch in this build context.
FROM golang:1.26.9-alpine@sha256:3082400e369fa24d5fc60bca20edab3f6d604e0c5a690ec66b295eff4dd87ade AS build
RUN apk add --no-cache git
ENV GOTOOLCHAIN=local
WORKDIR /src
RUN git clone --depth 1 --branch v0.50.0 https://github.com/paradigmxyz/iron-proxy.git . \
    && test "$(git rev-parse HEAD)" = 5bd11abeb95ca734c767cfc992ea9be862700614
COPY iron.patch /tmp/iron.patch
RUN git apply --check /tmp/iron.patch && git apply /tmp/iron.patch
COPY dependencies/go.mod dependencies/go.sum /src/
RUN go mod download && go mod verify
ENV GOFLAGS=-mod=readonly
COPY credential_gate.go credential_gate_test.go /src/internal/proxy/
RUN go test ./internal/proxy -run '^TestMAFCredential' -count=1
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /iron-proxy ./cmd/iron-proxy \
    && go version -m /iron-proxy > /iron-proxy-build-info.txt

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates-bundle \
    && apk del --no-cache apk-tools \
    && mkdir -p /run/maf-proxy /licenses \
    && chown 65532:65532 /run/maf-proxy
COPY --from=build /iron-proxy /usr/local/bin/iron-proxy
COPY --from=build /src/LICENSE /licenses/iron-proxy-LICENSE
COPY --from=build /usr/local/go/LICENSE /licenses/Go-LICENSE
COPY --from=build /iron-proxy-build-info.txt /usr/share/maf-egress-proxy/build-info.txt
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh
USER 65532:65532
EXPOSE 3128
ENTRYPOINT ["/entrypoint.sh"]
