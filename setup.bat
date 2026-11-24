@echo off
setlocal
rem The messages below are Japanese (UTF-8). Switch the console to UTF-8 first, and put the code page back at the end.
set "CF_CP="
for /f "tokens=2 delims=:." %%c in ('chcp') do set /a "CF_CP=%%c" >nul 2>&1
chcp 65001 >nul
rem ここから下は、UTF-8 として読まれる(日本語は、上の chcp の行より下にだけ書く)。echo の文の括弧は全角の
rem (）にする。chcp に失敗して CP932 として読まれたとき、「す^)」のような ^ が前の文字に食われ、括弧が
rem ブロックを閉じてしまうため。
rem 初回の用意: 仮想環境(.venv)を作り、依存するパッケージを入れる。埋め込みモデルは取得しない(無ければ、
rem 手元に置いたモデルのフォルダを尋ねる)。ダブルクリックで動く。
rem 使い方: setup.bat           (引数を付けると、終わりに pause しない)
rem 使える版: Python 3.11 から 3.14(3.15 からは、まだ onnxruntime の配布物が無い)。py ランチャーは、既定では
rem いちばん新しい版を選ぶので、使える版を新しい順に指定して探す。
set "RC=0"
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && for %%v in (3.14 3.13 3.12 3.11) do (
  if not defined PY py -%%v -c "pass" >nul 2>&1 && set "PY=py -%%v"
)
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo Python が見つかりません。
  echo https://www.python.org/downloads/ から Python 3.14（3.11 から 3.14 が使えます）を入れて、もう一度このファイルを実行してください。
  set "RC=1"
  goto :done
)
%PY% -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
if errorlevel 1 (
  echo Python 3.11 から 3.14 が要ります（3.15 からは、まだ onnxruntime の配布物がありません）。見つかった版:
  %PY% --version
  echo https://www.python.org/downloads/ から Python 3.14 を入れて、もう一度このファイルを実行してください。
  set "RC=1"
  goto :done
)
rem Python が消えたり入れ替わったりした .venv(使えない版の Python で作ったものも)は、作り直す。
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import pip, sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 || (
    echo == 仮想環境 .venv が壊れているか、使えない版の Python で作られています。作り直します
    %PY% -m venv --clear .venv || goto :failed
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo == 仮想環境を作ります: .venv
  %PY% -m venv .venv || goto :failed
)
echo == 依存するパッケージを入れます（初めては、PyPI から約 200 MB を取得します）
call :install || goto :failed
rem pip は入れた記録だけを見るので、読めなくなったパッケージ(.venv の Python が、同じフォルダで別の版に
rem 入れ替わった、パッケージが壊れた)を入れ直さない。そのときは .venv を 1 回だけ作り直す。
call :importable || (
  echo == 入れたパッケージを読み込めません（.venv の Python が入れ替わった見込み）。.venv を作り直します
  %PY% -m venv --clear .venv || goto :failed
  call :install || goto :failed
  call :importable || (
    echo パッケージは入りましたが、読み込めません。
    echo .venv のフォルダを消してから、もう一度このファイルを実行してください。
    echo それでも直らなければ、Python を入れ直してください。
    goto :failed
  )
)
".venv\Scripts\python.exe" -I "%~dp0ctxf.py" --run wizard setup || goto :failed
rem Copilot のプラグインは、MCP サーバーと hook を、py ではなくコマンド `python` で起動する。python.org の
rem インストーラーは、既定では `python` を PATH に足さない。そのとき `python` は Microsoft Store へのショートカット
rem (WindowsApps\python.exe)で、Python は起動しない。プラグインが動かないことを、はっきり知らせる。
python -c "pass" >nul 2>&1 || (
  echo.
  echo ======================================================================================
  echo 注意: ここではコマンド `python` で Python が起動しないので、Copilot のプラグインは動きません。
  echo プラグイン（register-copilot.bat）は、検索の道具と session の hook を `python` で起動します。
  echo このフォルダの .bat は、そのままで動きます。
  echo プラグインを直すには、次のどちらかをしてから、新しいコマンド プロンプトを開き、
  echo `python --version` で版が出ることを確かめてください:
  echo   - python.org のインストーラーをもう一度動かし、Modify を選んで
  echo     「Add Python to environment variables」を有効にする
  echo   - `where python` で ...\WindowsApps\python.exe が先に出るなら、それは Microsoft Store への
  echo     ショートカットで、Python ではありません。
  echo     設定 ^> アプリ ^> アプリの詳細設定 ^> アプリ実行エイリアス で、python.exe をオフにする
  echo ======================================================================================
)
goto :done
:install
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e ".[dev]" || exit /b 1
exit /b 0
:importable
".venv\Scripts\python.exe" -I -c "import numpy, onnxruntime, tokenizers, tree_sitter, jsonschema" >nul 2>&1
exit /b
:failed
echo 用意に失敗しました。わけは上の出力にあります。よくあるもの:
echo   「No matching distribution」: この Python かこの PC 向けの配布物が無いか、PyPI に届かなかった
echo   「No such file or directory」か「Long Path」: このフォルダのパスが長すぎる。短い場所に移してください
echo   接続・プロキシ・SSL の誤り: ネットワークかプロキシの設定（HTTPS_PROXY）を確かめてください
set "RC=1"
:done
rem 終わりに pause する(引数を付けたときはしない)。コンソールの文字コードを、始めのものに戻す。
if "%~1"=="" pause
if defined CF_CP if not "%CF_CP%"=="0" chcp %CF_CP% >nul
exit /b %RC%
