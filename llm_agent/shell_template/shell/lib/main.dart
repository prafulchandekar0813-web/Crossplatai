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

void main() => runApp(const ShellApp());

Color _hex(String s, Color fallback) {
  final m = RegExp(r'^#([0-9a-fA-F]{6})$').firstMatch(s);
  return m == null ? fallback : Color(0xFF000000 | int.parse(m.group(1)!, radix: 16));
}

class Info {
  Info(this.title, this.accent, this.announcement, this.backendUrl, this.pending, this.secondsLeft);
  final String title, accent, announcement, backendUrl;
  final bool pending;
  final int secondsLeft;

  factory Info.fromJson(Map<String, dynamic> j) {
    final s = (j['settings'] ?? {}) as Map<String, dynamic>;
    final u = (j['update'] ?? {}) as Map<String, dynamic>;
    return Info('${s['title'] ?? ''}', '${s['accent'] ?? ''}', '${s['announcement'] ?? ''}', '${s['backend_url'] ?? ''}',
        u['pending'] == true, (u['seconds_left'] ?? 0) as int);
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
  bool offline = false, loaded = false;
  Timer? timer;
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
    super.dispose();
  }

  Future<void> _boot() async {
    cfg = jsonDecode(await rootBundle.loadString('assets/app_config.json')) as Map<String, dynamic>;
    final prefs = await SharedPreferences.getInstance();
    deviceId = prefs.getString('device') ?? List.generate(24, (_) => Random.secure().nextInt(16).toRadixString(16)).join();
    await prefs.setString('device', deviceId);
    await _refresh();
  }

  Future<void> _refresh() async {
    final prefs = await SharedPreferences.getInstance();
    try {
      final r = await http.get(Uri.parse('${cfg['api_base']}/api/catalog/${cfg['app_id']}/products'), headers: {
        'X-Device-Id': deviceId,
        'X-App-Version': 'shell-1',
        'X-Platform': Platform.isAndroid ? 'android' : 'windows',
      }).timeout(const Duration(seconds: 12));
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
    setState(() => loaded = true);
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

  Widget _message(String title, String text, {VoidCallback? retry}) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(title, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600), textAlign: TextAlign.center),
            const SizedBox(height: 8),
            Text(text, textAlign: TextAlign.center),
            if (retry != null) ...[const SizedBox(height: 16), FilledButton(onPressed: retry, child: const Text('Try again'))],
          ]),
        ),
      );

  Widget _body(Info? i, String name, Color accent) {
    if (!loaded) return const Center(child: CircularProgressIndicator());
    if (i == null) return _message(name, 'Could not reach the app. Check your connection.', retry: _refresh);
    if (i.pending) return _message('Updating app', 'Please wait, or close and reopen in about ${max(1, countdown)} seconds.');
    if (i.backendUrl.isEmpty) return _message(name, 'This app is not set up yet. The owner needs to add its address in Manage app.');
    return Column(children: [
      if (i.announcement.isNotEmpty)
        Container(width: double.infinity, color: accent.withOpacity(0.12), padding: const EdgeInsets.all(10), child: Text(i.announcement, textAlign: TextAlign.center)),
      Expanded(
        child: InAppWebView(
          initialUrlRequest: URLRequest(url: WebUri(i.backendUrl)),
          onReceivedError: (c, req, err) {
            if (req.isForMainFrame ?? false) setState(() => offline = true);
          },
          onLoadStop: (c, u) => setState(() => offline = false),
        ),
      ),
      if (offline) Padding(padding: const EdgeInsets.all(8), child: TextButton(onPressed: _refresh, child: const Text('Connection problem. Tap to retry'))),
    ]);
  }
}
