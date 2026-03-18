/*
 * evaluator.cpp — Avaliador de mãos + Monte Carlo para PokerCalc
 */
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <array>
#include <vector>
#include <string>
#include <algorithm>
#include <random>
#include <stdexcept>
#include <numeric>

namespace py = pybind11;

// ─── Representação interna ────────────────────────────────
// Carta = inteiro 0-51: rank*4 + suit
static const std::string RANKS_STR = "23456789TJQKA";
static const std::string SUITS_STR = "shdc";

int card_to_int(const std::string& c) {
    if (c.size() < 2) throw std::invalid_argument("Carta inválida: " + c);
    int rank = (int)RANKS_STR.find(c[0]);
    int suit = (int)SUITS_STR.find(c[1]);
    if (rank == (int)std::string::npos || suit == (int)std::string::npos)
        throw std::invalid_argument("Carta inválida: " + c);
    return rank * 4 + suit;
}

inline int card_rank(int c) { return c / 4; }
inline int card_suit(int c) { return c % 4; }

// ─── Bitmasks para sequências ─────────────────────────────
static const uint32_t WHEEL_MASK = (1<<12)|(1<<0)|(1<<1)|(1<<2)|(1<<3);

// ─── Avaliador de 5 cartas ────────────────────────────────
struct HandVal {
    int cat, p1, p2, p3, p4, p5;
    bool operator>(const HandVal& o) const {
        if (cat!=o.cat) return cat>o.cat;
        if (p1!=o.p1)   return p1>o.p1;
        if (p2!=o.p2)   return p2>o.p2;
        if (p3!=o.p3)   return p3>o.p3;
        if (p4!=o.p4)   return p4>o.p4;
        return p5>o.p5;
    }
    bool operator==(const HandVal& o) const {
        return cat==o.cat&&p1==o.p1&&p2==o.p2&&p3==o.p3&&p4==o.p4&&p5==o.p5;
    }
    bool operator<(const HandVal& o)  const { return o>*this; }
    bool operator>=(const HandVal& o) const { return !(*this<o); }
};

HandVal eval5(int c0,int c1,int c2,int c3,int c4) {
    int r[5] = {card_rank(c0),card_rank(c1),card_rank(c2),card_rank(c3),card_rank(c4)};
    int s[5] = {card_suit(c0),card_suit(c1),card_suit(c2),card_suit(c3),card_suit(c4)};

    bool fl = (s[0]==s[1]&&s[1]==s[2]&&s[2]==s[3]&&s[3]==s[4]);

    uint32_t mask = (1u<<r[0])|(1u<<r[1])|(1u<<r[2])|(1u<<r[3])|(1u<<r[4]);
    bool st = false; int st_high = 0;
    if (mask == WHEEL_MASK) { st=true; st_high=3; }
    else {
        for (int i=1;i<=9;i++) {
            if (mask == (uint32_t)(0b11111u<<i)) { st=true; st_high=i+3; break; }
        }
    }

    int cnt[13] = {};
    for (int i=0;i<5;i++) cnt[r[i]]++;

    int fours=-1, three=-1, pairs[2]={-1,-1}; int pc=0;
    for (int rank=12;rank>=0;rank--) {
        if      (cnt[rank]==4) fours=rank;
        else if (cnt[rank]==3) three=rank;
        else if (cnt[rank]==2) { if(pc<2) pairs[pc++]=rank; }
    }

    if (st&&fl) return {8,st_high,0,0,0,0};
    if (fours>=0) {
        int k=-1; for(int rk=12;rk>=0;rk--) if(cnt[rk]==1){k=rk;break;}
        return {7,fours,k,0,0,0};
    }
    if (three>=0&&pc>0) return {6,three,pairs[0],0,0,0};
    if (fl) {
        int sr[5]; for(int i=0;i<5;i++) sr[i]=r[i];
        std::sort(sr,sr+5,std::greater<int>());
        return {5,sr[0],sr[1],sr[2],sr[3],sr[4]};
    }
    if (st) return {4,st_high,0,0,0,0};
    if (three>=0) {
        int k[2]={-1,-1}; int ki=0;
        for(int rk=12;rk>=0;rk--) if(cnt[rk]==1&&ki<2) k[ki++]=rk;
        return {3,three,k[0],k[1],0,0};
    }
    if (pc>=2) {
        int k=-1; for(int rk=12;rk>=0;rk--) if(cnt[rk]==1){k=rk;break;}
        int hi=std::max(pairs[0],pairs[1]), lo=std::min(pairs[0],pairs[1]);
        return {2,hi,lo,k,0,0};
    }
    if (pc==1) {
        int k[3]={-1,-1,-1}; int ki=0;
        for(int rk=12;rk>=0;rk--) if(cnt[rk]==1&&ki<3) k[ki++]=rk;
        return {1,pairs[0],k[0],k[1],k[2],0};
    }
    int sr[5]; for(int i=0;i<5;i++) sr[i]=r[i];
    std::sort(sr,sr+5,std::greater<int>());
    return {0,sr[0],sr[1],sr[2],sr[3],sr[4]};
}

// ─── Melhor mão de 7 cartas ───────────────────────────────
static const int COMBOS7[21][5] = {
    {0,1,2,3,4},{0,1,2,3,5},{0,1,2,3,6},{0,1,2,4,5},{0,1,2,4,6},
    {0,1,2,5,6},{0,1,3,4,5},{0,1,3,4,6},{0,1,3,5,6},{0,1,4,5,6},
    {0,2,3,4,5},{0,2,3,4,6},{0,2,3,5,6},{0,2,4,5,6},{0,3,4,5,6},
    {1,2,3,4,5},{1,2,3,4,6},{1,2,3,5,6},{1,2,4,5,6},{1,3,4,5,6},
    {2,3,4,5,6}
};

HandVal best7(const std::array<int,7>& cards) {
    HandVal best = eval5(cards[0],cards[1],cards[2],cards[3],cards[4]);
    for (int i=1;i<21;i++) {
        const int* c = COMBOS7[i];
        HandVal v = eval5(cards[c[0]],cards[c[1]],cards[c[2]],cards[c[3]],cards[c[4]]);
        if (v>best) best=v;
    }
    return best;
}

// ─── Monte Carlo ──────────────────────────────────────────
std::vector<double> monte_carlo(
    const std::vector<std::string>& hole_str,
    const std::vector<std::string>& board_str,
    int opponents,
    int n_sims
) {
    std::vector<int> hole_i, board_i, used;
    for (auto& c : hole_str)  { int v=card_to_int(c); hole_i.push_back(v);  used.push_back(v); }
    for (auto& c : board_str) { int v=card_to_int(c); board_i.push_back(v); used.push_back(v); }

    std::vector<int> deck;
    deck.reserve(52-(int)used.size());
    for (int i=0;i<52;i++)
        if (std::find(used.begin(),used.end(),i)==used.end()) deck.push_back(i);

    int need = 5 - (int)board_i.size();
    int wins=0, ties=0, losses=0;
    std::mt19937 rng(std::random_device{}());

    // Verifica que o deck tem cartas suficientes
    int cards_needed = need + opponents * 2;
    if ((int)deck.size() < cards_needed)
        throw std::runtime_error("Deck insuficiente para essa configuração");

    for (int sim=0;sim<n_sims;sim++) {
        std::shuffle(deck.begin(),deck.end(),rng);

        // Monta mão do herói: hole(2) + board_fixo + board_simulado
        std::array<int,7> hero;
        hero[0] = hole_i[0];
        hero[1] = hole_i[1];
        int idx = 2;
        for (int i=0;i<(int)board_i.size();i++) hero[idx++] = board_i[i];
        for (int i=0;i<need;i++)                hero[idx++] = deck[i];

        HandVal mine = best7(hero);

        // Avalia oponentes
        HandVal best_opp = {-1,0,0,0,0,0};
        for (int j=0;j<opponents;j++) {
            std::array<int,7> opp;
            opp[0] = deck[need + j*2];
            opp[1] = deck[need + j*2 + 1];
            int oidx = 2;
            for (int i=0;i<(int)board_i.size();i++) opp[oidx++] = board_i[i];
            for (int i=0;i<need;i++)                opp[oidx++] = deck[i];

            HandVal v = best7(opp);
            if (j==0||v>best_opp) best_opp=v;
        }

        if (mine>best_opp) wins++;
        else if (mine==best_opp) ties++;
        else losses++;
    }

    return {
        std::round(wins  *1000.0/n_sims)/10.0,
        std::round(ties  *1000.0/n_sims)/10.0,
        std::round(losses*1000.0/n_sims)/10.0
    };
}

// ─── Monte Carlo Multi ────────────────────────────────────
std::vector<std::vector<double>> monte_carlo_multi(
    const std::vector<std::vector<std::string>>& hands_str,
    const std::vector<std::string>& board_str,
    int n_sims
) {
    int nh = (int)hands_str.size();
    std::vector<std::vector<int>> hands_i(nh);
    std::vector<int> board_i, used;

    for (auto& c : board_str) { int v=card_to_int(c); board_i.push_back(v); used.push_back(v); }
    for (int i=0;i<nh;i++)
        for (auto& c : hands_str[i]) { int v=card_to_int(c); hands_i[i].push_back(v); used.push_back(v); }

    std::vector<int> deck;
    for (int i=0;i<52;i++)
        if (std::find(used.begin(),used.end(),i)==used.end()) deck.push_back(i);

    int need = 5-(int)board_i.size();
    std::vector<int> wins(nh,0), ties(nh,0), losses(nh,0);
    std::mt19937 rng(std::random_device{}());

    for (int sim=0;sim<n_sims;sim++) {
        std::shuffle(deck.begin(),deck.end(),rng);

        // Board completo simulado
        std::vector<int> sb = board_i;
        for (int i=0;i<need;i++) sb.push_back(deck[i]);

        // Avalia cada mão
        std::vector<HandVal> scores(nh);
        for (int i=0;i<nh;i++) {
            std::array<int,7> cards;
            cards[0]=hands_i[i][0]; cards[1]=hands_i[i][1];
            for (int j=0;j<5;j++) cards[2+j]=sb[j];
            scores[i]=best7(cards);
        }

        HandVal best=*std::max_element(scores.begin(),scores.end());
        std::vector<int> winners;
        for (int i=0;i<nh;i++) if(scores[i]==best) winners.push_back(i);

        if ((int)winners.size()==1) {
            wins[winners[0]]++;
            for (int i=0;i<nh;i++) if(i!=winners[0]) losses[i]++;
        } else {
            for (int w:winners) ties[w]++;
            for (int i=0;i<nh;i++)
                if (std::find(winners.begin(),winners.end(),i)==winners.end()) losses[i]++;
        }
    }

    std::vector<std::vector<double>> result(nh);
    for (int i=0;i<nh;i++)
        result[i]={
            std::round(wins[i]  *1000.0/n_sims)/10.0,
            std::round(ties[i]  *1000.0/n_sims)/10.0,
            std::round(losses[i]*1000.0/n_sims)/10.0
        };
    return result;
}

// ─── Bindings Python ─────────────────────────────────────
PYBIND11_MODULE(evaluator, m) {
    m.doc() = "PokerCalc — Monte Carlo em C++";
    m.def("monte_carlo",       &monte_carlo,
        py::arg("hole"), py::arg("board"), py::arg("opponents")=1, py::arg("n_sims")=5000);
    m.def("monte_carlo_multi", &monte_carlo_multi,
        py::arg("hands"), py::arg("board"), py::arg("n_sims")=5000);
}
