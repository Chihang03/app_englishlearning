/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: Object.fromEntries([
        "canvas", "surface", "ink", "muted", "line", "control-line", "control", "on-control",
        "control-disabled", "danger", "danger-line", "danger-bg", "success", "warning"
      ].map(name => [name, `var(--${name})`])),
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif"
        ]
      }
    }
  },
  plugins: []
};
