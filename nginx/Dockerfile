FROM nginx:1.26-alpine

RUN apk add --no-cache apache2-utils

# 删除默认配置
RUN rm /etc/nginx/conf.d/default.conf

# 注入env必须用template
COPY nginx/nginx.conf /etc/nginx/templates/nginx.conf.template
COPY --chmod=0755 nginx/10-metrics-auth.sh /docker-entrypoint.d/10-metrics-auth.sh
COPY static /app/static
RUN chmod -R a+rX /app/static

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
