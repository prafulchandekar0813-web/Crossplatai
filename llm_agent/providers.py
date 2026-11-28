"""Established services the assistant may suggest when an app needs hosting, a database, sign-in, files or payments.
A starting point, not an endorsement: the assistant is told to check current pricing and availability with web search."""
from typing import Dict, List

PROVIDERS: Dict[str, List[str]] = {
    "Hosting for a Flask/Python backend": ["Render", "Railway", "Fly.io", "PythonAnywhere", "Google Cloud Run", "AWS App Runner"],
    "Database": ["Supabase (Postgres)", "Neon (Postgres)", "MongoDB Atlas", "Turso (SQLite)", "Firebase Firestore", "a local SQLite file for small apps"],
    "Sign-in / user accounts": ["Supabase Auth", "Firebase Authentication", "Auth0", "Clerk", "your own Flask-Login with a hashed-password table"],
    "File and image storage": ["Cloudflare R2", "AWS S3", "Supabase Storage", "Cloudinary"],
    "Payments": ["Stripe", "Razorpay", "PayPal"],
    "Email and notifications": ["Resend", "SendGrid", "Firebase Cloud Messaging"],
}


def prompt_text() -> str:
    return "; ".join(f"{k}: {', '.join(v)}" for k, v in PROVIDERS.items())
