FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Tokyo \
    LANG=ja_JP.UTF-8 \
    LC_ALL=ja_JP.UTF-8 \
    HOME=/root \
    LO_PROFILE=/root/.config/libreoffice

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-draw \
      libreoffice-core libreoffice-common libreoffice-java-common \
      fonts-noto-cjk fonts-noto \
      python3-uno \
      python3 python3-pip locales tzdata tini ca-certificates \
      coreutils findutils file grep sed gawk \
    && sed -i 's/# ja_JP.UTF-8 UTF-8/ja_JP.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY convert.py /usr/local/bin/convert.py
RUN chmod +x /usr/local/bin/convert.py && mkdir -p "${LO_PROFILE}"

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python3","/usr/local/bin/convert.py"]
