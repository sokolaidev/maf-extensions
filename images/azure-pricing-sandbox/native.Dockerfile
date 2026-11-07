ARG PRICING_IMAGE
FROM ${PRICING_IMAGE}
USER 0
RUN apk add --no-cache bash python3 util-linux coreutils
RUN mkdir -p /maf-sandbox /run /tmp
