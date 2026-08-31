import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Allows Cloudflare Quick Tunnel URLs (*.trycloudflare.com) through --
    // Vite blocks unrecognized Host headers by default. This is fine for a
    // temporary demo tunnel; tighten to your specific tunnel hostname
    // instead of the wildcard if you want to be more restrictive.
    allowedHosts: ['.trycloudflare.com'],
  },
})