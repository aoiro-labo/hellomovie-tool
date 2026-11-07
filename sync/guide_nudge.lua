-- guide_nudge.lua - mpv 用のキーボード操作スクリプト
--
-- 再生中のガイドのずれをホットキーで微調整できるようにします。
-- 補正値 (秒) は script-opts で指定されたファイルに書き出され、
-- 同期ツール (hellomovie_sync.py) が 0.25 秒ごとに読み取って
-- ガイドの再生位置を追い込みます。
--
--   Shift+Right / Shift+Left : ±5 秒
--   Alt+Right  / Alt+Left   : ±0.5 秒
--   Ctrl+Right / Ctrl+Left  : ±0.1 秒
--   Ctrl+0                 : 0 に戻す
--
-- 使い方:
--   mpv --script=guide_nudge.lua --script-opts=guide_nudge-offset_file=/tmp/offset.txt video.mkv

local mp = require 'mp'

-- 補正値を書き出すファイルのパスは --script-opts で受け取る
-- (mp.options の read_options は設定ファイルが無いと nil を返すため、
--  script-opts プロパティを直接読む)
local script_opts = mp.get_property_native('script-opts') or {}
local offset_file = script_opts['guide_nudge-offset_file'] or ''
local offset = 0.0

local function publish()
    if offset_file == '' then
        return
    end
    local file = io.open(offset_file, 'w')
    if file ~= nil then
        file:write(string.format('%.3f', offset))
        file:close()
    end
end

local function nudge(delta)
    offset = offset + delta
    publish()
    mp.osd_message(string.format('ガイド補正: %+.2f 秒', offset))
end

mp.add_key_binding('Shift+RIGHT', 'guide-nudge-plus5', function() nudge(5) end)
mp.add_key_binding('Shift+LEFT', 'guide-nudge-minus5', function() nudge(-5) end)
mp.add_key_binding('Alt+RIGHT', 'guide-nudge-plus05', function() nudge(0.5) end)
mp.add_key_binding('Alt+LEFT', 'guide-nudge-minus05', function() nudge(-0.5) end)
mp.add_key_binding('Ctrl+RIGHT', 'guide-nudge-plus01', function() nudge(0.1) end)
mp.add_key_binding('Ctrl+LEFT', 'guide-nudge-minus01', function() nudge(-0.1) end)
mp.add_key_binding('Ctrl+0', 'guide-nudge-reset', function()
    offset = 0
    publish()
    mp.osd_message('ガイド補正: 0.00 秒')
end)

-- 外部 (GUI や別のツール) からの補正指示
mp.register_script_message('nudge', function(delta)
    nudge(tonumber(delta) or 0)
end)

publish()
