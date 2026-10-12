FROM nginx:1.30.4-alpine

# The edge controller is Python, just like the service. It can control only its
# own Nginx master; ACME/account/private-cache handling stays in certificate-agent.
RUN apk add --no-cache python3 ca-certificates

# Remove the bundled default server.
RUN rm /etc/nginx/conf.d/default.conf

# Keep runtime environment substitution in Nginx templates and classic-builder
# compatibility for the operator's normal Compose build command.
COPY nginx/nginx.conf /etc/nginx/templates/nginx.conf.template
COPY nginx/15-select-tls.sh /docker-entrypoint.d/15-select-tls.sh
COPY nginx/19-maintenance-gate.sh /docker-entrypoint.d/19-maintenance-gate.sh
COPY nginx/25-ip-security.sh /docker-entrypoint.d/25-ip-security.sh
COPY nginx/22-automatic-tls.sh /docker-entrypoint.d/22-automatic-tls.sh
COPY nginx/26-automatic-tls.sh /docker-entrypoint.d/26-automatic-tls.sh
COPY nginx/tls_edge.py /usr/local/lib/frontiercloud/tls_edge.py
COPY nginx/render-security.awk /etc/nginx/render-security.awk
COPY nginx/security-headers.conf /etc/nginx/security-headers.conf
COPY nginx/karaoke-security-headers.conf /etc/nginx/karaoke-security-headers.conf
COPY nginx/maintenance-gate.conf /etc/nginx/maintenance-gate.conf
COPY nginx/maintenance.html /usr/share/nginx/html/maintenance.html
COPY nginx/transport /etc/nginx/transport
COPY static /app/static
RUN chmod 0755 \
        /docker-entrypoint.d/15-select-tls.sh \
        /docker-entrypoint.d/19-maintenance-gate.sh \
        /docker-entrypoint.d/25-ip-security.sh && \
    chmod 0755 /docker-entrypoint.d/22-automatic-tls.sh /docker-entrypoint.d/26-automatic-tls.sh && \
    chmod -R a+rX /app/static /usr/share/nginx/html/maintenance.html

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
