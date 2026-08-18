ARG SOURCE_IMAGE
FROM ${SOURCE_IMAGE}

USER root
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        -u http_proxy -u https_proxy -u all_proxy \
        apt-get -o Acquire::Retries=8 \
            -o Acquire::http::Timeout=120 \
            -o Acquire::https::Timeout=120 update \
    && env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        -u http_proxy -u https_proxy -u all_proxy \
        DEBIAN_FRONTEND=noninteractive \
        apt-get -o Acquire::Retries=8 \
            -o Acquire::http::Timeout=120 \
            -o Acquire::https::Timeout=120 install -y --no-install-recommends \
            imagemagick ffmpeg libfreetype6-dev libqhull-dev pkg-config \
            texlive texlive-latex-extra texlive-fonts-recommended \
            texlive-xetex texlive-luatex cm-super dvipng \
    && rm -rf /var/lib/apt/lists/*
