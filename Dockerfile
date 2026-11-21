FROM nginx:alpine@sha256:fb4658584bd9a916ffb6f6e365f97c25cfdb2923801f551ea08356e695f14ff7

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html styles.css game.mjs game-engine.mjs /usr/share/nginx/html/
COPY pilot-ssh-native.txt /usr/share/nginx/html/

HEALTHCHECK CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
