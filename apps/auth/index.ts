import { betterAuth } from "better-auth";
import { Hono } from "hono";
import { cors } from "hono/cors";

export const auth = betterAuth({
  database: {
    type: "postgres",
    url: process.env.DATABASE_URL!,
  },
  emailAndPassword: { enabled: true },
});

const app = new Hono();
app.use("/*", cors());
app.on(["POST", "GET"], "/api/auth/**", (c) => auth.handler(c.req.raw));

export default { port: 3001, fetch: app.fetch };
