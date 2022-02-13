echo "1" | tee keep_minting_flag

. ./venv/bin/activate
reai-nft mint-in-batch-no-stop --batchsize=200 --fee=1500> minting.out 2>&1 &