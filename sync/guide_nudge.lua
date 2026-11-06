-- guide_nudge.lua - mpv 用のキーボード操作スクリプト
--
-- 再生中のガイドのずれを hotter key で微調整できるようにします。
-- guide-offset プロパティにcorrection秒を保持し、同期ツール (hellomovie_sync.py) が
-- 0.25秒ごとに読み取ってガイドの再生位置を追い込みます。
--
--   Shift+Right / Shift+Left : ±5 秒
--   Alt+Right  / Alt+Left   : ±0.5 秒
--   Ctrl+Right / Ctrl+Left  : ±0.1 秒
--   Ctrl+0                 : 0 に戻す
--
local mp = require 'mp'

mp.add_property("guide-offset", "number", 0.0)

local function nudge(delta)
    local value = mp.get_property_number("guide-offset", 0) + delta
    mp.set_property_number("guide-offset", value)
    mp.osd_message(string.format("ガイド補正: %+.2f 秒", value))
end

mp.add_key_binding("Shift+RIGHT", "guide-nudge-plus5", function() nudge(5) end)
mp.add_key_binding("Shift+LEFT", "guide-nudge-minus5", function() nudge(-5) end)
mp.add_key_binding("Alt+RIGHT", "guide-nudge-plus05", function() nudge(0.5) end)
mp.add_key_binding("Alt+LEFT", "guide-nudge-minus05", function() nudge(-0.5) end)
mp.add_key_binding("Ctrl+RIGHT", "guide-nudge-plus01", function() nudge(0.1) end)
mp.add_key_binding("Ctrl+LEFT", "guide-nudge-minus01", function() nudge(-0.1) end)
mp.add_key_binding("Ctrl+0", "guide-nudge-reset", function()
    mp.set_property_number("guide-offset", 0)
    mp.osd_message("ガイド補正: 0.00 秒")
end)
