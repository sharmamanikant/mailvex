FROM node:20-alpine AS build

WORKDIR /app
ARG APP_BUILD_REV=campaign-editor-20260825
COPY package*.json ./
RUN npm ci
COPY . .
RUN echo $APP_BUILD_REV
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
