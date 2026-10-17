import { ClerkProvider } from "@clerk/nextjs";

// Clerk loads only for sign-in, sign-up and the app; the public pages stay free of it.
const appearance = {
  variables: {
    colorPrimary: "#e9e7e2",
    colorPrimaryForeground: "#000000",
    colorBackground: "#0d0d0d",
    colorForeground: "#e9e7e2",
    colorMutedForeground: "#8a8a86",
    colorInput: "#151515",
    colorInputForeground: "#e9e7e2",
    colorBorder: "#2a2a2a",
    colorNeutral: "#e9e7e2",
    colorDanger: "#ff6e5e",
    colorSuccess: "#8fd19e",
    colorWarning: "#f0b43c",
    borderRadius: "10px",
    fontFamily: "var(--font-schibsted), Helvetica Neue, Arial, sans-serif",
  },
};

export default function AccountLayout({ children }: { children: React.ReactNode }) {
  return (
    <ClerkProvider
      appearance={appearance}
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      signInFallbackRedirectUrl="/app"
      signUpFallbackRedirectUrl="/app"
    >
      {children}
    </ClerkProvider>
  );
}
