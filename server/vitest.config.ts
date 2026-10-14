import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        bindings: {
          CYRIL_ADMIN_TOKEN: "admin", CYRIL_AGENT_KEY: "agent",
          IMAGEKIT_PRIVATE_KEY: "private_test", IMAGEKIT_URL_ENDPOINT: "https://ik.imagekit.io/test",
        },
      },
    }),
  ],
});
