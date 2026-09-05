/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      colors: {
        brand: {
          50: "#effaf7",
          100: "#d5f2e9",
          200: "#aee4d4",
          300: "#78cdb6",
          400: "#40ae93",
          500: "#238e76",
          600: "#16735f",
          700: "#155c4e",
          800: "#144a40",
          900: "#123e36",
          950: "#082923",
        },
      },
      animation: {
        "fade-up": "fadeUp 0.25s ease both",
        "slide-in": "slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) both",
        shimmer: "shimmer 1.8s ease-in-out infinite",
      },
      keyframes: {
        fadeUp: {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        slideIn: {
          from: { opacity: "0", transform: "translateX(16px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      boxShadow: {
        xs: "0 1px 2px 0 rgba(15, 23, 42, 0.05)",
        card: "0 1px 2px 0 rgba(15, 23, 42, 0.04), 0 1px 1px 0 rgba(15, 23, 42, 0.03)",
        popover: "0 12px 32px -8px rgba(15, 23, 42, 0.16), 0 0 0 1px rgba(15, 23, 42, 0.04)",
        glow: "0 0 0 4px rgba(99, 102, 241, 0.1)",
      },
      backgroundImage: {
        "brand-gradient": "linear-gradient(135deg, #155c4e 0%, #238e76 100%)",
      },
    },
  },
  plugins: [],
};
