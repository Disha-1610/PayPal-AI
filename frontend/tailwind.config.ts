import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/features/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        paypal: {
          blue: "#003087",
          darkblue: "#001C64",
          lightblue: "#0079C1",
          yellow: "#FFC439",
        },
      },
      keyframes: {
        float: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-14px)" },
        },
        marquee: {
          from: { transform: "translateX(0)" },
          to: { transform: "translateX(-50%)" },
        },
        blob: {
          "0%, 100%": { transform: "translate(0,0) scale(1)" },
          "50%": { transform: "translate(40px,-30px) scale(1.15)" },
        },
        pulseRing: {
          "0%": { transform: "scale(0.9)", opacity: "0.6" },
          "100%": { transform: "scale(1.8)", opacity: "0" },
        },
      },
      animation: {
        float: "float 6s ease-in-out infinite",
        "float-slow": "float 9s ease-in-out infinite",
        marquee: "marquee 28s linear infinite",
        blob: "blob 14s ease-in-out infinite",
        "pulse-ring": "pulseRing 2.4s ease-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;

