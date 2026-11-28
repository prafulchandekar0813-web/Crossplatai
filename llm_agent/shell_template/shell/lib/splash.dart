// The screen shown while the app opens, wakes up its server, updates or cannot connect.
// The logo is the one the owner chose when building the app. The "Powered by" line at the bottom is fixed: it is part of
// the system, so it is on every one of these screens and the owner's settings cannot remove it.
import 'package:flutter/material.dart';

class Splash extends StatefulWidget {
  const Splash({super.key, required this.brand, required this.accent, this.title = '', this.text = '', this.busy = false, this.action});
  final String brand, title, text;
  final Color accent;
  final bool busy;
  final Widget? action;

  @override
  State<Splash> createState() => _SplashState();
}

class _SplashState extends State<Splash> with SingleTickerProviderStateMixin {
  // Opening animation: the logo grows in and fades up, then the text appears under it.
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1100))..forward();
  late final Animation<double> _scale = Tween<double>(begin: 0.6, end: 1.0).animate(CurvedAnimation(parent: _c, curve: Curves.easeOutBack));
  late final Animation<double> _fade = CurvedAnimation(parent: _c, curve: const Interval(0, 0.6, curve: Curves.easeOut));
  late final Animation<double> _late = CurvedAnimation(parent: _c, curve: const Interval(0.5, 1, curve: Curves.easeIn));

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(context).colorScheme.onSurface.withOpacity(0.55);
    return Column(children: [
      Expanded(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              FadeTransition(
                opacity: _fade,
                child: ScaleTransition(
                  scale: _scale,
                  child: Image.asset('assets/logo.png', width: 112, height: 112,
                      errorBuilder: (_, __, ___) => Icon(Icons.apps_rounded, size: 96, color: widget.accent)),
                ),
              ),
              const SizedBox(height: 24),
              FadeTransition(
                opacity: _late,
                child: Column(children: [
                  if (widget.title.isNotEmpty) Text(widget.title, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600), textAlign: TextAlign.center),
                  if (widget.text.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 8), child: Text(widget.text, textAlign: TextAlign.center)),
                  if (widget.busy)
                    Padding(
                      padding: const EdgeInsets.only(top: 20),
                      child: SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2.5, color: widget.accent)),
                    ),
                  if (widget.action != null) Padding(padding: const EdgeInsets.only(top: 16), child: widget.action),
                ]),
              ),
            ]),
          ),
        ),
      ),
      Padding(padding: const EdgeInsets.only(bottom: 18), child: Text('Powered by ${widget.brand}', style: TextStyle(fontSize: 12.5, color: muted))),
    ]);
  }
}
