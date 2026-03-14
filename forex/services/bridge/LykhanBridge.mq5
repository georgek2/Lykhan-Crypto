//+------------------------------------------------------------------+
//|  LykhanBridge.mq5                                                |
//|  Lykhan Forex Agent — MT5 File Bridge Expert Advisor v1.10       |
//|                                                                  |
//|  FILE SYSTEM NOTE                                                |
//|  ─────────────────                                               |
//|  MT5 sandboxes all file I/O to the terminal's MQL5\Files\       |
//|  directory. Absolute paths like C:\mt5bridge\ are silently      |
//|  blocked. This EA uses RELATIVE paths, which MT5 automatically  |
//|  resolves to: <Terminal Data Folder>\MQL5\Files\mt5bridge\       |
//|                                                                  |
//|  On the Linux/Bottles side, Python points its bridge to the     |
//|  same folder via the equivalent Linux path.                      |
//|                                                                  |
//|  INSTALLATION                                                    |
//|  ────────────                                                    |
//|  1. Copy to MT5 Data Folder → MQL5\Experts\                     |
//|  2. Compile in MetaEditor (F4 → open file → F7)                 |
//|  3. Attach to EURUSD chart                                       |
//|  4. Enable "Allow automated trading" in Common tab               |
//|  5. Enable Algo Trading button in main MT5 toolbar               |
//+------------------------------------------------------------------+
#property copyright "Lykhan Forex Agent"
#property version   "1.10"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//── Input Parameters ──────────────────────────────────────────────────────────
// SubFolder is RELATIVE to MT5's MQL5\Files\ directory.
// Do NOT use absolute paths like C:\... — MT5's sandbox will block them.
input string SubFolder  = "mt5bridge";  // Bridge folder name inside MQL5\Files\
input int    PollMs     = 500;          // How often to check for commands (ms)
input bool   VerboseLog = true;         // Print detailed logs to MT5 journal

//── Global state ──────────────────────────────────────────────────────────────
CTrade        Trade;
CPositionInfo PosInfo;

string CmdDir    = "";
string ResDir    = "";
string StatusDir = "";
string HbFile    = "";

datetime LastHeartbeat = 0;

//+------------------------------------------------------------------+
//| OnInit — runs once when EA is attached to chart                  |
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

   Print("[LykhanBridge] v1.10 initialised. Folder: MQL5\\Files\\", SubFolder,
         " — polling every ", PollMs, "ms");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnDeinit — runs when EA is removed from chart                    |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[LykhanBridge] Stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| OnTimer — fires every PollMs milliseconds                        |
//+------------------------------------------------------------------+
void OnTimer()
{
   WriteHeartbeat(false);
   ProcessCommandFiles();
}

//+------------------------------------------------------------------+
//| Write heartbeat.txt every 5 seconds so Python knows EA is alive  |
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
      if(VerboseLog && force) Print("[LykhanBridge] Heartbeat written: ", HbFile);
   }
   else
      Print("[LykhanBridge] WARNING: Failed to write heartbeat. Error=", GetLastError());
}

//+------------------------------------------------------------------+
//| Scan commands directory for pending JSON files                   |
//+------------------------------------------------------------------+
void ProcessCommandFiles()
{
   string fname  = "";
   long   handle = FileFindFirst(CmdDir + "cmd_*.json", fname);
   if(handle == INVALID_HANDLE) return;

   do
   {
      ProcessSingleCommand(CmdDir + fname);
   }
   while(FileFindNext(handle, fname));

   FileFindClose(handle);
}

//+------------------------------------------------------------------+
//| Read one command file, parse JSON, dispatch to handler           |
//+------------------------------------------------------------------+
void ProcessSingleCommand(const string filePath)
{
   int fh = FileOpen(filePath, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(fh == INVALID_HANDLE)
   {
      if(VerboseLog) Print("[LykhanBridge] Cannot open: ", filePath, " err=", GetLastError());
      return;
   }

   string json = "";
   while(!FileIsEnding(fh))
      json += FileReadString(fh);
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
   int    ticket    = (int)JsonGetDouble(json, "ticket");

   Trade.SetExpertMagicNumber(magic);
   Trade.SetDeviationInPoints(slippage);

   // Delete command file immediately to prevent double-processing
   FileDelete(filePath);

   if     (action == "BUY")        ExecuteBuy(commandId, symbol, lotSize, slPips, tpPips, comment);
   else if(action == "SELL")       ExecuteSell(commandId, symbol, lotSize, slPips, tpPips, comment);
   else if(action == "CLOSE")      ExecuteClose(commandId, ticket, symbol);
   else if(action == "CLOSE_ALL")  ExecuteCloseAll(commandId, magic);
   else if(action == "GET_STATUS") ExecuteGetStatus(commandId);
   else                            WriteError(commandId, -1, "Unknown action: " + action);
}

//+------------------------------------------------------------------+
//| Open a market BUY position                                       |
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
   if(ok) WriteSuccess(cid, (int)Trade.ResultOrder(), Trade.ResultPrice(), 0, 0);
   else   WriteError(cid, (int)Trade.ResultRetcode(), Trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| Open a market SELL position                                      |
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
   if(ok) WriteSuccess(cid, (int)Trade.ResultOrder(), Trade.ResultPrice(), 0, 0);
   else   WriteError(cid, (int)Trade.ResultRetcode(), Trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| Close a specific open position by ticket number                  |
//+------------------------------------------------------------------+
void ExecuteClose(const string cid, int ticket, const string sym)
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
   else WriteError(cid, -2, "Ticket not found: " + IntegerToString(ticket));
}

//+------------------------------------------------------------------+
//| Close ALL positions matching the given magic number              |
//+------------------------------------------------------------------+
void ExecuteCloseAll(const string cid, int magic)
{
   int closed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket))
         if((int)PositionGetInteger(POSITION_MAGIC) == magic || magic == 0)
         { Trade.PositionClose(ticket); closed++; }
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
//| Write full account snapshot to status directory                  |
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
      posArray += "{\"ticket\":"        + IntegerToString((int)ticket)
               +  ",\"symbol\":\""     + PositionGetString(POSITION_SYMBOL)                        + "\""
               +  ",\"action\":\""     + pt                                                         + "\""
               +  ",\"lot_size\":"     + DoubleToString(PositionGetDouble(POSITION_VOLUME),    2)
               +  ",\"open_price\":"   + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5)
               +  ",\"current_price\":"+ DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5)
               +  ",\"sl\":"           + DoubleToString(PositionGetDouble(POSITION_SL),        5)
               +  ",\"tp\":"           + DoubleToString(PositionGetDouble(POSITION_TP),        5)
               +  ",\"profit\":"       + DoubleToString(PositionGetDouble(POSITION_PROFIT),    2)
               +  ",\"swap\":"         + DoubleToString(PositionGetDouble(POSITION_SWAP),      2)
               +  ",\"magic\":"        + IntegerToString((int)PositionGetInteger(POSITION_MAGIC))
               +  ",\"comment\":\""    + PositionGetString(POSITION_COMMENT)                        + "\""
               +  ",\"open_time\":\""  + TimeToString((datetime)PositionGetInteger(POSITION_TIME),
                                           TIME_DATE|TIME_SECONDS)                                   + "\""
               +  "}";
   }
   posArray += "]";

   string snapshot = "{\"balance\":"      + DoubleToString(balance,    2)
                   + ",\"equity\":"       + DoubleToString(equity,     2)
                   + ",\"margin\":"       + DoubleToString(margin,     2)
                   + ",\"free_margin\":"  + DoubleToString(freeMargin, 2)
                   + ",\"margin_level\":" + DoubleToString(marginLvl,  2)
                   + ",\"profit\":"       + DoubleToString(profit,     2)
                   + ",\"positions\":"    + posArray
                   + ",\"snapshot_time\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";

   string statusFile = StatusDir + "status_" + cid + ".json";
   int fh = FileOpen(statusFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE) { FileWriteString(fh, snapshot); FileClose(fh); }
}

//+------------------------------------------------------------------+
//| Write EXECUTED result file                                       |
//+------------------------------------------------------------------+
void WriteSuccess(const string cid, int ticket, double openPrice,
                  double closePrice, double profit)
{
   string json = "{\"command_id\":\"" + cid + "\","
               + "\"status\":\"EXECUTED\","
               + "\"ticket\":"      + IntegerToString(ticket)      + ","
               + "\"open_price\":"  + DoubleToString(openPrice, 5) + ","
               + "\"close_price\":" + DoubleToString(closePrice,5) + ","
               + "\"profit\":"      + DoubleToString(profit,    2) + ","
               + "\"error_code\":null,\"error_message\":null,"
               + "\"processed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
   WriteResultFile(cid, json);
}

//+------------------------------------------------------------------+
//| Write REJECTED / ERROR result file                               |
//+------------------------------------------------------------------+
void WriteError(const string cid, int code, const string msg)
{
   string escaped = msg;
   StringReplace(escaped, "\"", "'");
   string json = "{\"command_id\":\"" + cid + "\","
               + "\"status\":\"REJECTED\","
               + "\"ticket\":null,\"open_price\":null,\"close_price\":null,\"profit\":null,"
               + "\"error_code\":"     + IntegerToString(code) + ","
               + "\"error_message\":\"" + escaped              + "\","
               + "\"processed_at\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
   WriteResultFile(cid, json);
   Print("[LykhanBridge] Error ", code, ": ", msg);
}

//+------------------------------------------------------------------+
//| Write any result JSON to the results sub-directory              |
//+------------------------------------------------------------------+
void WriteResultFile(const string cid, const string json)
{
   string path = ResDir + "res_" + cid + ".json";
   int fh = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE)
   {
      FileWriteString(fh, json);
      FileClose(fh);
      if(VerboseLog) Print("[LykhanBridge] Result → ", path);
   }
   else Print("[LykhanBridge] FAILED to write result: ", path, " err=", GetLastError());
}

//+------------------------------------------------------------------+
//| Extract a string value from flat JSON by key                     |
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
//| Extract a numeric value from flat JSON by key                    |
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
