//+------------------------------------------------------------------+
//|  LykhanBridge.mq5                                                |
//|  Lykhan Forex Agent — MT5 File Bridge Expert Advisor v1.30       |
//|                                                                  |
//|  v1.30 additions:                                                |
//|    - GET_CANDLES command: returns OHLCV bars for any symbol      |
//|      and timeframe as a JSON array. Used by the Python           |
//|      strategic LLM analysis loop and HFT scanner.               |
//|                                                                  |
//|  v1.20 fix: ticket numbers changed from int to ulong to handle   |
//|  Deriv's large 64-bit ticket numbers without integer overflow.   |
//+------------------------------------------------------------------+
#property copyright "Lykhan Forex Agent"
#property version   "1.30"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

input string SubFolder  = "mt5bridge";
input int    PollMs     = 500;
input bool   VerboseLog = true;

CTrade        Trade;
CPositionInfo PosInfo;

string CmdDir    = "";
string ResDir    = "";
string StatusDir = "";
string HbFile    = "";

datetime LastHeartbeat = 0;

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   CmdDir    = SubFolder + "\\commands\\";
   ResDir    = SubFolder + "\\results\\";
   StatusDir = SubFolder + "\\status\\";
   HbFile    = SubFolder + "\\heartbeat.txt";

   FolderCreate(SubFolder,    0);
   FolderCreate(CmdDir,       0);
   FolderCreate(ResDir,       0);
   FolderCreate(StatusDir,    0);

   WriteHeartbeat(true);
   EventSetMillisecondTimer(PollMs);

   Print("[LykhanBridge] v1.30 initialised. Folder: MQL5\\Files\\", SubFolder,
         " — polling every ", PollMs, "ms");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[LykhanBridge] Stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| OnTimer                                                          |
//+------------------------------------------------------------------+
void OnTimer()
{
   WriteHeartbeat(false);
   ProcessCommandFiles();
}

//+------------------------------------------------------------------+
//| Heartbeat                                                        |
//+------------------------------------------------------------------+
void WriteHeartbeat(bool force)
{
   datetime now = TimeCurrent();
   if(!force && (now - LastHeartbeat < 5)) return;
   LastHeartbeat = now;

   int fh = FileOpen(HbFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE)
   {
      FileWriteString(fh, TimeToString(now, TIME_DATE | TIME_SECONDS));
      FileClose(fh);
   }
   else Print("[LykhanBridge] WARNING: Failed to write heartbeat. Error=", GetLastError());
}

//+------------------------------------------------------------------+
//| Scan commands directory                                          |
//+------------------------------------------------------------------+
void ProcessCommandFiles()
{
   string fname  = "";
   long   handle = FileFindFirst(CmdDir + "cmd_*.json", fname);
   if(handle == INVALID_HANDLE) return;

   do { ProcessSingleCommand(CmdDir + fname); }
   while(FileFindNext(handle, fname));

   FileFindClose(handle);
}

//+------------------------------------------------------------------+
//| Parse and dispatch one command                                   |
//+------------------------------------------------------------------+
void ProcessSingleCommand(const string filePath)
{
   int fh = FileOpen(filePath, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(fh == INVALID_HANDLE) return;

   string json = "";
   while(!FileIsEnding(fh)) json += FileReadString(fh);
   FileClose(fh);

   if(VerboseLog) Print("[LykhanBridge] Command: ", json);

   string commandId = JsonGetString(json, "command_id");
   string action    = JsonGetString(json, "action");
   string symbol    = JsonGetString(json, "symbol");
   double lotSize   = JsonGetDouble(json, "lot_size");
   int    slPips    = (int)JsonGetDouble(json, "sl_pips");
   int    tpPips    = (int)JsonGetDouble(json, "tp_pips");
   int    slippage  = (int)JsonGetDouble(json, "slippage");
   int    magic     = (int)JsonGetDouble(json, "magic");
   string comment   = JsonGetString(json, "comment");
   ulong  ticket    = (ulong)JsonGetDouble(json, "ticket");

   // GET_CANDLES fields
   string timeframe = JsonGetString(json, "timeframe");
   int    count     = (int)JsonGetDouble(json, "count");
   if(count <= 0) count = 100;

   Trade.SetExpertMagicNumber(magic);
   Trade.SetDeviationInPoints(slippage);

   FileDelete(filePath);

   if     (action == "BUY")         ExecuteBuy(commandId, symbol, lotSize, slPips, tpPips, comment);
   else if(action == "SELL")        ExecuteSell(commandId, symbol, lotSize, slPips, tpPips, comment);
   else if(action == "CLOSE")       ExecuteClose(commandId, ticket, symbol);
   else if(action == "CLOSE_ALL")   ExecuteCloseAll(commandId, magic);
   else if(action == "GET_STATUS")  ExecuteGetStatus(commandId);
   else if(action == "GET_CANDLES") ExecuteGetCandles(commandId, symbol, timeframe, count);
   else                             WriteError(commandId, -1, "Unknown action: " + action);
}

//+------------------------------------------------------------------+
//| GET_CANDLES — returns OHLCV bars as JSON to results/             |
//+------------------------------------------------------------------+
void ExecuteGetCandles(const string cid, const string sym,
                       const string tfStr, int count)
{
   ENUM_TIMEFRAMES period = StringToTimeframe(tfStr);

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(sym, period, 0, count, rates);

   if(copied <= 0)
   {
      WriteError(cid, -3, "CopyRates failed: " + sym + " " + tfStr +
                           " err=" + IntegerToString(GetLastError()));
      return;
   }

   // Build JSON: most recent bar last (Python expects oldest→newest)
   string bars = "[";
   for(int i = copied - 1; i >= 0; i--)
   {
      if(i < copied - 1) bars += ",";
      bars += "{"
           + "\"time\":\""  + TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS) + "\","
           + "\"open\":"    + DoubleToString(rates[i].open,  5) + ","
           + "\"high\":"    + DoubleToString(rates[i].high,  5) + ","
           + "\"low\":"     + DoubleToString(rates[i].low,   5) + ","
           + "\"close\":"   + DoubleToString(rates[i].close, 5) + ","
           + "\"volume\":"  + IntegerToString(rates[i].tick_volume)
           + "}";
   }
   bars += "]";

   string payload = "{"
                  + "\"symbol\":\""    + sym    + "\","
                  + "\"timeframe\":\"" + tfStr  + "\","
                  + "\"bars\":"        + bars
                  + "}";

   string path = ResDir + "res_" + cid + ".json";
   int fh = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE) { FileWriteString(fh, payload); FileClose(fh); }
   else Print("[LykhanBridge] FAILED to write candle result: ", path);

   if(VerboseLog)
      Print("[LykhanBridge] GET_CANDLES: ", sym, " ", tfStr,
            " copied=", copied, " → ", path);
}

//+------------------------------------------------------------------+
//| Map timeframe string to ENUM_TIMEFRAMES                          |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(const string tf)
{
   if(tf == "M1")  return PERIOD_M1;
   if(tf == "M5")  return PERIOD_M5;
   if(tf == "M15") return PERIOD_M15;
   if(tf == "H1")  return PERIOD_H1;
   if(tf == "H4")  return PERIOD_H4;
   if(tf == "D1")  return PERIOD_D1;
   if(tf == "W1")  return PERIOD_W1;
   if(tf == "MN1") return PERIOD_MN1;
   Print("[LykhanBridge] Unknown timeframe: ", tf, " — defaulting to H1");
   return PERIOD_H1;
}

//+------------------------------------------------------------------+
//| BUY                                                              |
//+------------------------------------------------------------------+
void ExecuteBuy(const string cid, const string sym, double lots,
                int slPips, int tpPips, const string cmt)
{
   double price  = SymbolInfoDouble(sym, SYMBOL_ASK);
   double point  = SymbolInfoDouble(sym, SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double sl = (slPips > 0) ? NormalizeDouble(price - slPips * point * 10, digits) : 0;
   double tp = (tpPips > 0) ? NormalizeDouble(price + tpPips * point * 10, digits) : 0;
   bool ok = Trade.Buy(lots, sym, price, sl, tp, cmt);
   if(ok) WriteSuccess(cid, Trade.ResultOrder(), Trade.ResultPrice(), 0, 0);
   else   WriteError(cid, (int)Trade.ResultRetcode(), Trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| SELL                                                             |
//+------------------------------------------------------------------+
void ExecuteSell(const string cid, const string sym, double lots,
                 int slPips, int tpPips, const string cmt)
{
   double price  = SymbolInfoDouble(sym, SYMBOL_BID);
   double point  = SymbolInfoDouble(sym, SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double sl = (slPips > 0) ? NormalizeDouble(price + slPips * point * 10, digits) : 0;
   double tp = (tpPips > 0) ? NormalizeDouble(price - tpPips * point * 10, digits) : 0;
   bool ok = Trade.Sell(lots, sym, price, sl, tp, cmt);
   if(ok) WriteSuccess(cid, Trade.ResultOrder(), Trade.ResultPrice(), 0, 0);
   else   WriteError(cid, (int)Trade.ResultRetcode(), Trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| CLOSE by ticket                                                  |
//+------------------------------------------------------------------+
void ExecuteClose(const string cid, ulong ticket, const string sym)
{
   if(PositionSelectByTicket(ticket))
   {
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double profit    = PositionGetDouble(POSITION_PROFIT);
      bool   ok        = Trade.PositionClose(ticket);
      if(ok)
      {
         double closePrice = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                             ? SymbolInfoDouble(sym, SYMBOL_BID)
                             : SymbolInfoDouble(sym, SYMBOL_ASK);
         WriteSuccess(cid, ticket, openPrice, closePrice, profit);
      }
      else WriteError(cid, (int)Trade.ResultRetcode(), Trade.ResultRetcodeDescription());
   }
   else WriteError(cid, -2, "Ticket not found: " + IntegerToString((long)ticket));
}

//+------------------------------------------------------------------+
//| CLOSE_ALL                                                        |
//+------------------------------------------------------------------+
void ExecuteCloseAll(const string cid, int magic)
{
   int closed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t))
         if((int)PositionGetInteger(POSITION_MAGIC) == magic || magic == 0)
         { Trade.PositionClose(t); closed++; }
   }
   string json = "{\"command_id\":\"" + cid + "\","
               + "\"status\":\"CLOSED\","
               + "\"ticket\":null,\"open_price\":null,\"close_price\":null,\"profit\":0,"
               + "\"error_code\":null,"
               + "\"error_message\":\"Closed " + IntegerToString(closed) + " positions\","
               + "\"processed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
   WriteResultFile(cid, json);
}

//+------------------------------------------------------------------+
//| GET_STATUS                                                       |
//+------------------------------------------------------------------+
void ExecuteGetStatus(const string cid)
{
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin     = AccountInfoDouble(ACCOUNT_MARGIN);
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double marginLvl  = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);
   double profit     = AccountInfoDouble(ACCOUNT_PROFIT);

   string posArray = "[";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(i > 0) posArray += ",";
      string pt = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      posArray += "{"
               + "\"ticket\":"         + IntegerToString((long)ticket)                              + ","
               + "\"symbol\":\""       + PositionGetString(POSITION_SYMBOL)                         + "\","
               + "\"action\":\""       + pt                                                          + "\","
               + "\"lot_size\":"       + DoubleToString(PositionGetDouble(POSITION_VOLUME),      2) + ","
               + "\"open_price\":"     + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),  5) + ","
               + "\"current_price\":"  + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5) + ","
               + "\"sl\":"             + DoubleToString(PositionGetDouble(POSITION_SL),          5) + ","
               + "\"tp\":"             + DoubleToString(PositionGetDouble(POSITION_TP),          5) + ","
               + "\"profit\":"         + DoubleToString(PositionGetDouble(POSITION_PROFIT),      2) + ","
               + "\"swap\":"           + DoubleToString(PositionGetDouble(POSITION_SWAP),        2) + ","
               + "\"magic\":"          + IntegerToString((int)PositionGetInteger(POSITION_MAGIC))   + ","
               + "\"comment\":\""      + PositionGetString(POSITION_COMMENT)                        + "\","
               + "\"open_time\":\""    + TimeToString((datetime)PositionGetInteger(POSITION_TIME),
                                          TIME_DATE|TIME_SECONDS)                                    + "\""
               + "}";
   }
   posArray += "]";

   string snapshot = "{"
                   + "\"balance\":"       + DoubleToString(balance,    2) + ","
                   + "\"equity\":"        + DoubleToString(equity,     2) + ","
                   + "\"margin\":"        + DoubleToString(margin,     2) + ","
                   + "\"free_margin\":"   + DoubleToString(freeMargin, 2) + ","
                   + "\"margin_level\":"  + DoubleToString(marginLvl,  2) + ","
                   + "\"profit\":"        + DoubleToString(profit,     2) + ","
                   + "\"positions\":"     + posArray                       + ","
                   + "\"snapshot_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\""
                   + "}";

   string statusFile = StatusDir + "status_" + cid + ".json";
   int fh = FileOpen(statusFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE) { FileWriteString(fh, snapshot); FileClose(fh); }
}

//+------------------------------------------------------------------+
//| Write EXECUTED result                                            |
//+------------------------------------------------------------------+
void WriteSuccess(const string cid, ulong ticket, double openPrice,
                  double closePrice, double profit)
{
   string json = "{\"command_id\":\"" + cid + "\","
               + "\"status\":\"EXECUTED\","
               + "\"ticket\":"      + IntegerToString((long)ticket) + ","
               + "\"open_price\":"  + DoubleToString(openPrice, 5)  + ","
               + "\"close_price\":" + DoubleToString(closePrice,5)  + ","
               + "\"profit\":"      + DoubleToString(profit,    2)  + ","
               + "\"error_code\":null,\"error_message\":null,"
               + "\"processed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
   WriteResultFile(cid, json);
}

//+------------------------------------------------------------------+
//| Write REJECTED result                                            |
//+------------------------------------------------------------------+
void WriteError(const string cid, int code, const string msg)
{
   string escaped = msg;
   StringReplace(escaped, "\"", "'");
   string json = "{\"command_id\":\"" + cid + "\","
               + "\"status\":\"REJECTED\","
               + "\"ticket\":null,\"open_price\":null,\"close_price\":null,\"profit\":null,"
               + "\"error_code\":"      + IntegerToString(code) + ","
               + "\"error_message\":\"" + escaped               + "\","
               + "\"processed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
   WriteResultFile(cid, json);
   Print("[LykhanBridge] Error ", code, ": ", msg);
}

//+------------------------------------------------------------------+
//| Write result file to results/                                    |
//+------------------------------------------------------------------+
void WriteResultFile(const string cid, const string json)
{
   string path = ResDir + "res_" + cid + ".json";
   int fh = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE) { FileWriteString(fh, json); FileClose(fh); }
   else Print("[LykhanBridge] FAILED to write result: ", path, " err=", GetLastError());
}

//+------------------------------------------------------------------+
//| JSON string parser                                               |
//+------------------------------------------------------------------+
string JsonGetString(const string json, const string key)
{
   string search = "\"" + key + "\":\"";
   int start = StringFind(json, search);
   if(start == -1) return "";
   start += StringLen(search);
   int end = StringFind(json, "\"", start);
   if(end == -1) return "";
   return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
//| JSON number parser                                               |
//+------------------------------------------------------------------+
double JsonGetDouble(const string json, const string key)
{
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start == -1) return 0;
   start += StringLen(search);
   while(start < StringLen(json) && StringSubstr(json, start, 1) == " ") start++;
   if(StringSubstr(json, start, 4) == "null") return 0;
   bool quoted = (StringSubstr(json, start, 1) == "\"");
   if(quoted) start++;
   int end = start;
   while(end < StringLen(json))
   {
      string ch = StringSubstr(json, end, 1);
      if(ch == "," || ch == "}" || ch == "]" || ch == "\"" || ch == "\n") break;
      end++;
   }
   return StringToDouble(StringSubstr(json, start, end - start));
}