FROM nginx:1.26-alpine

# Remove the bundled default server.
RUN rm /etc/nginx/conf.d/default.conf

# Keep runtime environment substitution in Nginx templates.
COPY nginx/nginx.conf /etc/nginx/templates/nginx.conf.template
COPY --chmod=0755 nginx/15-select-tls.sh /docker-entrypoint.d/15-select-tls.sh
COPY nginx/environments /etc/nginx/environments
COPY static /app/static
RUN chmod -R a+rX /app/static

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
