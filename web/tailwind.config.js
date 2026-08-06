/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#1b1e2b",
        console: "#0d1017",
        primary: "#4fd1c5",
        secondary: "#2b6cb0",
        tertiary: "#9ae6b4",
        surface: "#2d3748",
        input: "#1f2334",
        border: "rgba(255,255,255,0.08)",
        textPrimary: "#f7fafc",
        textSecondary: "#a0aec0",
        error: "#fc8181",
        warning: "#f6ad55",
      },
      borderRadius: {
        DEFAULT: "0.25rem",
        lg: "0.5rem",
        xl: "0.75rem",
      },
      fontFamily: {
        headline: ["Space Grotesk", "sans-serif"],
        body: ["Inter", "sans-serif"],
        label: ["Public Sans", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
    },
  },
  plugins: [],
};
