---
name: playbook-booking
description: Playbook for booking and appointment apps (salon, clinic, tutor, trainer, repair, events): screens, navigation, palette options, signature features, manage.json sections, sources.
---
Use this together with app-design. Study and adapt it.

WHO IT IS FOR: customers who want to see services, pick a time in a few taps and get a confirmation; owners who want requests in their Inbox.

SCREENS (Android, bottom tabs): Home, Services, Book, My bookings, Contact.
- Home: hero with the business name and a single "Book now" button, a "next available" chip, service highlights with icons, a team row with photos, ratings and testimonials, opening hours and a map button.
- Services: cards with picture, name, duration, price and a Book button; categories as chips.
- Book: a stepper with 4 steps (service, date and time, details, confirm); a horizontal date strip and time-slot chips (disabled when taken); a summary card before confirming.
- My bookings: upcoming and past, with Reschedule and Cancel.
WINDOWS: a side rail, services list on the left with the booking panel beside it (date picker and slots), and the summary on the right.

PALETTE OPTIONS: calm teal #0f766e on #f0fdfa with a coral #fb7185 accent; or charcoal #111827 with gold #d4a017 for a premium salon. Rounded cards, generous spacing.

SIGNATURE FEATURES: slot chips, a "next available" shortcut, WhatsApp and call buttons, reminders text, cancellation policy line, loyalty or package offers.

manage.json SECTIONS: services (name, duration, price, description, image), team (name, role, photo), hours, testimonials, bookings (inbox: guest, phone, service, date, time, status).

SOURCES TO READ with read_url: Material Design 3 (m3.material.io) for date and time pickers and chips; Fluent 2 (fluent2.microsoft.design) for the Windows layout; open-source booking UIs on GitHub with an MIT or Apache licence for the flow of steps.
