// Branded shell: shows the app the owner manages in the workbench.
// Everything it shows comes from the workbench catalogue (title, colour, announcement, address, update notice),
// so the owner changes it from Manage app and the installed app follows without a rebuild.
import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'splash.dart';

void main() => runApp(const ShellApp());

Color _hex(String s, Color fallback) {
  final m = RegExp(r'^#([0-9a-fA-F]{6})$').firstMatch(s);
  return m == null ? fallback : Color(0xFF000000 | int.parse(m.group(1)!, radix: 16));
}

class Info {
  Info(this.title, this.accent, this.announcement, this.backendUrl, this.pending, this.secondsLeft, this.brand);
  final String title, accent, announcement, backendUrl, brand;
  final bool pending;
  final int secondsLeft;

  factory Info.fromJson(Map<String, dynamic> j) {
    final s = (j['settings'] ?? {}) as Map<String, dynamic>;
    final u = (j['update'] ?? {}) as Map<String, dynamic>;
    return Info('${s['title'] ?? ''}', '${s['accent'] ?? ''}', '${s['announcement'] ?? ''}', '${s['backend_url'] ?? ''}',
        u['pending'] == true, (u['seconds_left'] ?? 0) as int, '${j['brand'] ?? ''}');
  }
}

class ShellApp extends StatefulWidget {
  const ShellApp({super.key});
  @override
  State<ShellApp> createState() => _ShellAppState();
}

class _ShellAppState extends State<ShellApp> {
  Map<String, dynamic> cfg = {};
  Info? info;
  bool offline = false, loaded = false, waking = false, pageReady = false;
  Timer? timer, wakeTimer;
  int countdown = 0;
  String deviceId = '';

  @override
  void initState() {
    super.initState();
    _boot();
  }

  @override
  void dispose() {
    timer?.cancel();
    wakeTimer?.cancel();
    super.dispose();
  }

  Future<void> _boot() async {
    cfg = jsonDecode(await rootBundle.loadString('assets/app_config.json')) as Map<String, dynamic>;
    final prefs = await SharedPreferences.getInstance();
    deviceId = prefs.getString('device') ?? List.generate(24, (_) => Random.secure().nextInt(16).toRadixString(16)).join();
    await prefs.setString('device', deviceId);
    // Show the opening animation for at least a moment, and say so if the server is asleep and slow to answer.
    final minimum = Future.delayed(const Duration(milliseconds: 1600));
    wakeTimer = Timer(const Duration(seconds: 4), () {
      if (!loaded) setState(() => waking = true);
    });
    await _refresh();
    await minimum;
    wakeTimer?.cancel();
    setState(() => loaded = true);
  }

  String get brand => (info?.brand ?? '').isNotEmpty ? info!.brand : '${cfg['brand'] ?? 'Agent Workbench'}';

  Future<void> _refresh() async {
    final prefs = await SharedPreferences.getInstance();
    try {
      final r = await http.get(Uri.parse('${cfg['api_base']}/api/catalog/${cfg['app_id']}/products'), headers: {
        'X-Device-Id': deviceId,
        'X-App-Version': 'shell-1',
        'X-Platform': Platform.isAndroid ? 'android' : 'windows',
      }).timeout(const Duration(seconds: 30));   // a sleeping server needs a while to wake up
      if (r.statusCode != 200) throw Exception('status ${r.statusCode}');
      await prefs.setString('cache', r.body);
      info = Info.fromJson(jsonDecode(r.body) as Map<String, dynamic>);
      offline = false;
    } catch (_) {
      final c = prefs.getString('cache');
      info = c == null ? null : Info.fromJson(jsonDecode(c) as Map<String, dynamic>);
      offline = info == null;
    }
    timer?.cancel();
    if (info != null && info!.pending) {
      countdown = info!.secondsLeft;
      timer = Timer.periodic(const Duration(seconds: 1), (t) {
        if (--countdown <= 0) {
          t.cancel();
          _refresh();
        } else {
          setState(() {});
        }
      });
    }
    setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final i = info;
    final accent = _hex(i?.accent ?? '', _hex('${cfg['accent'] ?? ''}', const Color(0xFF2563EB)));
    final name = (i != null && i.title.isNotEmpty) ? i.title : '${cfg['name'] ?? 'App'}';
    return MaterialApp(
      title: name,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: accent), useMaterial3: true),
      home: Scaffold(body: SafeArea(child: _body(i, name, accent))),
    );
  }

  Widget _splash(Color accent, {String title = '', String text = '', bool busy = false, VoidCallback? retry}) => Splash(
        brand: brand,
        accent: accent,
        title: title,
        text: text,
        busy: busy,
        action: retry == null ? null : FilledButton(onPressed: retry, child: const Text('Try again')),
      );

  Widget _body(Info? i, String name, Color accent) {
    if (!loaded) return _splash(accent, text: waking ? 'Waking up the app. This can take up to half a minute.' : '', busy: true);
    if (i == null) return _splash(accent, title: name, text: 'Could not reach the app. Check your connection.', retry: _refresh);
    if (i.pending) return _splash(accent, title: 'Updating app', text: 'Please wait, or close and reopen in about ${max(1, countdown)} seconds.', busy: true);
    if (i.backendUrl.isEmpty) return _splash(accent, title: name, text: 'This app is not set up yet. The owner needs to add its address in Manage app.');
    return Column(children: [
      if (i.announcement.isNotEmpty)
        Container(width: double.infinity, color: accent.withOpacity(0.12), padding: const EdgeInsets.all(10), child: Text(i.announcement, textAlign: TextAlign.center)),
      Expanded(
        // The loading screen stays over the page until it has loaded, so a sleeping server never shows a blank screen.
        child: Stack(children: [
          InAppWebView(
            initialUrlRequest: URLRequest(url: WebUri(i.backendUrl)),
            onReceivedError: (c, req, err) {
              if (req.isForMainFrame ?? false) setState(() => offline = pageReady = true);
            },
            onLoadStop: (c, u) => setState(() {
              offline = false;
              pageReady = true;
            }),
          ),
          if (!pageReady)
            Positioned.fill(
              child: Builder(
                builder: (ctx) => ColoredBox(
                  color: Theme.of(ctx).scaffoldBackgroundColor,
                  child: _splash(accent, text: 'Opening the app. If it was asleep this can take a little while.', busy: true),
                ),
              ),
            ),
        ]),
      ),
      if (offline) Padding(padding: const EdgeInsets.all(8), child: TextButton(onPressed: _refresh, child: const Text('Connection problem. Tap to retry'))),
    ]);
  }
}
